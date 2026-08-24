"""Durable at-least-once handoff between the HTTP receiver and index worker."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal

from issue_graphrag.live.models import RepoEvent
from issue_graphrag.live.timeutil import parse_iso, to_iso

DeliveryStatus = Literal["pending", "processing", "succeeded", "failed"]
SemanticJobStatus = Literal["pending", "processing", "deferred"]
EnqueueOutcome = Literal["enqueued", "duplicate", "requeued"]
SemanticEnqueueOutcome = Literal["enqueued", "existing", "replaced"]


class DeliveryConflict(ValueError):
    """A delivery id was reused for different GitHub input."""


class LeaseLostError(RuntimeError):
    """A worker tried to commit after its processing lease was replaced."""


@dataclass(frozen=True)
class EnqueueResult:
    delivery_id: str
    outcome: EnqueueOutcome


@dataclass(frozen=True)
class InboxDelivery:
    event: RepoEvent
    status: DeliveryStatus
    attempts: int
    enqueued_at: str
    updated_at: str
    claimed_at: str | None
    lease_id: str | None
    next_attempt_at: str
    completed_at: str | None
    last_error: str | None


@dataclass(frozen=True)
class SemanticJob:
    document_id: str
    content_signature: str
    semantic_namespace: str
    trigger_delivery_id: str
    status: SemanticJobStatus
    next_unit_index: int
    total_units: int
    attempts: int
    enqueued_at: str
    updated_at: str
    claimed_at: str | None
    lease_id: str | None
    next_attempt_at: str
    last_error: str | None


def event_fingerprint(event: RepoEvent) -> str:
    """Identity of GitHub's input, excluding local arrival and enrichment fields."""
    payload = {
        "event_type": event.event_type,
        "action": event.action,
        "repo": event.repo,
        "source": event.source,
        "payload": event.payload,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_json(event: RepoEvent) -> str:
    return json.dumps(event.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DeliveryInbox:
    """A small SQLite queue with leases, retries and delivery-id idempotency.

    A single processing lease is intentional. The live state is one local JSON
    document, so concurrent workers would create a lost-update race. SQLite
    serializes claiming; expensive extraction happens outside the transaction.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'processing', 'succeeded', 'failed')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    enqueued_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    lease_id TEXT,
                    next_attempt_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS deliveries_ready
                    ON deliveries(status, next_attempt_at, enqueued_at, delivery_id);
                CREATE TABLE IF NOT EXISTS semantic_jobs (
                    document_id TEXT PRIMARY KEY,
                    content_signature TEXT NOT NULL,
                    semantic_namespace TEXT NOT NULL DEFAULT '',
                    trigger_delivery_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'processing', 'deferred')
                    ),
                    next_unit_index INTEGER NOT NULL DEFAULT 0 CHECK (next_unit_index >= 0),
                    total_units INTEGER NOT NULL CHECK (total_units >= 0),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    enqueued_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    lease_id TEXT,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS semantic_jobs_ready
                    ON semantic_jobs(status, next_attempt_at, updated_at, document_id);
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(deliveries)").fetchall()
            }
            if "lease_id" not in columns:
                connection.execute("ALTER TABLE deliveries ADD COLUMN lease_id TEXT")
            semantic_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(semantic_jobs)").fetchall()
            }
            if "semantic_namespace" not in semantic_columns:
                connection.execute(
                    "ALTER TABLE semantic_jobs "
                    "ADD COLUMN semantic_namespace TEXT NOT NULL DEFAULT ''"
                )

    @staticmethod
    def _record(row: sqlite3.Row | None) -> InboxDelivery | None:
        if row is None:
            return None
        return InboxDelivery(
            event=RepoEvent.model_validate(json.loads(row["event_json"])),
            status=row["status"],
            attempts=int(row["attempts"]),
            enqueued_at=row["enqueued_at"],
            updated_at=row["updated_at"],
            claimed_at=row["claimed_at"],
            lease_id=row["lease_id"],
            next_attempt_at=row["next_attempt_at"],
            completed_at=row["completed_at"],
            last_error=row["last_error"],
        )

    @staticmethod
    def _semantic_record(row: sqlite3.Row | None) -> SemanticJob | None:
        if row is None:
            return None
        return SemanticJob(
            document_id=row["document_id"],
            content_signature=row["content_signature"],
            semantic_namespace=row["semantic_namespace"],
            trigger_delivery_id=row["trigger_delivery_id"],
            status=row["status"],
            next_unit_index=int(row["next_unit_index"]),
            total_units=int(row["total_units"]),
            attempts=int(row["attempts"]),
            enqueued_at=row["enqueued_at"],
            updated_at=row["updated_at"],
            claimed_at=row["claimed_at"],
            lease_id=row["lease_id"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
        )

    def enqueue(self, event: RepoEvent, now: str) -> EnqueueResult:
        """Persist a delivery before acknowledging it to GitHub."""
        moment = to_iso(now)
        fingerprint = event_fingerprint(event)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT fingerprint, status FROM deliveries WHERE delivery_id = ?",
                (event.delivery_id,),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise DeliveryConflict(
                        f"delivery {event.delivery_id!r} was reused with a different payload"
                    )
                if existing["status"] == "failed":
                    connection.execute(
                        """
                        UPDATE deliveries
                        SET status = 'pending', attempts = 0, updated_at = ?,
                            claimed_at = NULL, lease_id = NULL,
                            next_attempt_at = ?, completed_at = NULL,
                            last_error = NULL
                        WHERE delivery_id = ?
                        """,
                        (moment, moment, event.delivery_id),
                    )
                    return EnqueueResult(event.delivery_id, "requeued")
                return EnqueueResult(event.delivery_id, "duplicate")

            connection.execute(
                """
                INSERT INTO deliveries (
                    delivery_id, repo, event_type, action, fingerprint, event_json,
                    status, attempts, enqueued_at, updated_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
                """,
                (
                    event.delivery_id,
                    event.repo,
                    event.event_type,
                    event.action,
                    fingerprint,
                    _event_json(event),
                    moment,
                    moment,
                    moment,
                ),
            )
        return EnqueueResult(event.delivery_id, "enqueued")

    def get(self, delivery_id: str) -> InboxDelivery | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return self._record(row)

    def count(self, status: DeliveryStatus | None = None) -> int:
        query = "SELECT COUNT(*) FROM deliveries"
        params: tuple[str, ...] = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        with self._connect() as connection:
            return int(connection.execute(query, params).fetchone()[0])

    def list_deliveries(
        self,
        status: DeliveryStatus | None = None,
        limit: int = 20,
    ) -> list[InboxDelivery]:
        query = "SELECT * FROM deliveries"
        params: list[str | int] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY enqueued_at DESC, delivery_id DESC LIMIT ?"
        params.append(max(0, limit))
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [record for row in rows if (record := self._record(row)) is not None]

    def claim_next(
        self,
        now: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> InboxDelivery | None:
        """Claim the oldest ready delivery, reclaiming an expired worker lease."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        moment = to_iso(now)
        stale_before = to_iso(parse_iso(moment) - timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE deliveries
                SET status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END,
                    updated_at = ?, claimed_at = NULL, lease_id = NULL,
                    next_attempt_at = CASE WHEN attempts >= ? THEN next_attempt_at ELSE ? END,
                    completed_at = CASE WHEN attempts >= ? THEN ? ELSE NULL END,
                    last_error = CASE
                        WHEN attempts >= ? THEN COALESCE(last_error, 'processing lease expired')
                        ELSE last_error
                    END
                WHERE status = 'processing' AND claimed_at <= ?
                """,
                (
                    max_attempts,
                    moment,
                    max_attempts,
                    moment,
                    max_attempts,
                    moment,
                    max_attempts,
                    stale_before,
                ),
            )

            # One lease protects the single JSON state from concurrent writers.
            if connection.execute(
                "SELECT 1 FROM deliveries WHERE status = 'processing' LIMIT 1"
            ).fetchone():
                return None
            if connection.execute(
                "SELECT 1 FROM semantic_jobs WHERE status = 'processing' LIMIT 1"
            ).fetchone():
                return None

            row = connection.execute(
                """
                SELECT delivery_id FROM deliveries
                WHERE status = 'pending' AND attempts < ? AND next_attempt_at <= ?
                ORDER BY enqueued_at, delivery_id
                LIMIT 1
                """,
                (max_attempts, moment),
            ).fetchone()
            if row is None:
                return None

            lease_id = uuid.uuid4().hex
            connection.execute(
                """
                UPDATE deliveries
                SET status = 'processing', attempts = attempts + 1,
                    claimed_at = ?, lease_id = ?, updated_at = ?, completed_at = NULL
                WHERE delivery_id = ?
                """,
                (moment, lease_id, moment, row["delivery_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (row["delivery_id"],)
            ).fetchone()
        return self._record(claimed)

    def update_event(self, event: RepoEvent, lease_id: str, now: str) -> None:
        """Persist deterministic enrichment and the assigned index clock."""
        moment = to_iso(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fingerprint, status, lease_id FROM deliveries WHERE delivery_id = ?",
                (event.delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown delivery: {event.delivery_id}")
            if row["fingerprint"] != event_fingerprint(event):
                raise DeliveryConflict(
                    f"delivery {event.delivery_id!r} was updated with a different payload"
                )
            if row["status"] != "processing" or row["lease_id"] != lease_id:
                raise LeaseLostError(f"processing lease lost for {event.delivery_id!r}")
            cursor = connection.execute(
                """
                UPDATE deliveries SET event_json = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (_event_json(event), moment, event.delivery_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"processing lease lost for {event.delivery_id!r}")

    def renew_lease(self, delivery_id: str, lease_id: str, now: str) -> None:
        """Heartbeat an active lease while extraction or API I/O is running."""
        moment = to_iso(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries SET claimed_at = ?, updated_at = ?
                WHERE delivery_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (moment, moment, delivery_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"processing lease lost for {delivery_id!r}")

    def mark_succeeded(self, delivery_id: str, lease_id: str, now: str) -> None:
        moment = to_iso(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries
                SET status = 'succeeded', updated_at = ?, completed_at = ?,
                    claimed_at = NULL, lease_id = NULL, last_error = NULL
                WHERE delivery_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (moment, moment, delivery_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"processing lease lost for {delivery_id!r}")

    def mark_failed(
        self,
        delivery_id: str,
        lease_id: str,
        error: str,
        now: str,
        retry_delay_seconds: int,
        max_attempts: int,
    ) -> Literal["retrying", "failed"]:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        moment = to_iso(now)
        retry_at = to_iso(parse_iso(moment) + timedelta(seconds=retry_delay_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempts, status FROM deliveries
                WHERE delivery_id = ? AND lease_id = ?
                """,
                (delivery_id, lease_id),
            ).fetchone()
            if row is None or row["status"] != "processing":
                raise LeaseLostError(f"processing lease lost for {delivery_id!r}")
            terminal = int(row["attempts"]) >= max_attempts
            status = "failed" if terminal else "pending"
            connection.execute(
                """
                UPDATE deliveries
                SET status = ?, updated_at = ?, claimed_at = NULL, lease_id = NULL,
                    next_attempt_at = ?, completed_at = ?, last_error = ?
                WHERE delivery_id = ? AND lease_id = ?
                """,
                (
                    status,
                    moment,
                    retry_at,
                    moment if terminal else None,
                    error[:4000],
                    delivery_id,
                    lease_id,
                ),
            )
        return "failed" if terminal else "retrying"

    def upsert_semantic_job(
        self,
        *,
        document_id: str,
        content_signature: str,
        trigger_delivery_id: str,
        total_units: int,
        now: str,
        semantic_namespace: str = "",
    ) -> SemanticEnqueueOutcome:
        """Make one document/content version durably eligible for enrichment."""
        if total_units < 0:
            raise ValueError("total_units must be non-negative")
        moment = to_iso(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT content_signature, semantic_namespace, status
                FROM semantic_jobs WHERE document_id = ?""",
                (document_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO semantic_jobs (
                        document_id, content_signature, semantic_namespace,
                        trigger_delivery_id, status,
                        next_unit_index, total_units, attempts, enqueued_at, updated_at,
                        next_attempt_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, ?, 0, ?, ?, ?)
                    """,
                    (
                        document_id,
                        content_signature,
                        semantic_namespace,
                        trigger_delivery_id,
                        total_units,
                        moment,
                        moment,
                        moment,
                    ),
                )
                return "enqueued"

            if (
                existing["content_signature"] == content_signature
                and existing["semantic_namespace"] == semantic_namespace
            ):
                connection.execute(
                    """
                    UPDATE semantic_jobs
                    SET total_units = ?, updated_at = ?
                    WHERE document_id = ?
                    """,
                    (total_units, moment, document_id),
                )
                return "existing"

            if existing["status"] == "processing":
                raise LeaseLostError(
                    f"cannot replace active semantic work for {document_id!r}"
                )
            connection.execute(
                """
                UPDATE semantic_jobs
                SET content_signature = ?, semantic_namespace = ?,
                    trigger_delivery_id = ?, status = 'pending',
                    next_unit_index = 0, total_units = ?, attempts = 0,
                    enqueued_at = ?, updated_at = ?, claimed_at = NULL, lease_id = NULL,
                    next_attempt_at = ?, last_error = NULL
                WHERE document_id = ?
                """,
                (
                    content_signature,
                    semantic_namespace,
                    trigger_delivery_id,
                    total_units,
                    moment,
                    moment,
                    moment,
                    document_id,
                ),
            )
        return "replaced"

    def get_semantic_job(self, document_id: str) -> SemanticJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM semantic_jobs WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return self._semantic_record(row)

    def list_semantic_jobs(self, limit: int = 20) -> list[SemanticJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM semantic_jobs
                ORDER BY updated_at, document_id
                LIMIT ?
                """,
                (max(0, limit),),
            ).fetchall()
        return [record for row in rows if (record := self._semantic_record(row))]

    def count_semantic_jobs(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM semantic_jobs").fetchone()[0])

    def claim_semantic_job(self, now: str, lease_seconds: int) -> SemanticJob | None:
        """Claim one deferred document without racing the source-delivery lane."""
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        moment = to_iso(now)
        stale_before = to_iso(parse_iso(moment) - timedelta(seconds=lease_seconds))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE semantic_jobs
                SET status = 'deferred', updated_at = ?, claimed_at = NULL,
                    lease_id = NULL, next_attempt_at = ?,
                    last_error = COALESCE(last_error, 'semantic lease expired')
                WHERE status = 'processing' AND claimed_at <= ?
                """,
                (moment, moment, stale_before),
            )

            if connection.execute(
                "SELECT 1 FROM deliveries WHERE status = 'processing' LIMIT 1"
            ).fetchone():
                return None
            if connection.execute(
                "SELECT 1 FROM semantic_jobs WHERE status = 'processing' LIMIT 1"
            ).fetchone():
                return None
            # Source observations always outrank enrichment. Do not start a
            # model call while a ready GitHub delivery is waiting for the lane.
            if connection.execute(
                """
                SELECT 1 FROM deliveries
                WHERE status = 'pending' AND next_attempt_at <= ?
                LIMIT 1
                """,
                (moment,),
            ).fetchone():
                return None

            row = connection.execute(
                """
                SELECT document_id FROM semantic_jobs
                WHERE status IN ('pending', 'deferred') AND next_attempt_at <= ?
                ORDER BY attempts, updated_at, document_id
                LIMIT 1
                """,
                (moment,),
            ).fetchone()
            if row is None:
                return None
            lease_id = uuid.uuid4().hex
            connection.execute(
                """
                UPDATE semantic_jobs
                SET status = 'processing', attempts = attempts + 1,
                    claimed_at = ?, lease_id = ?, updated_at = ?
                WHERE document_id = ?
                """,
                (moment, lease_id, moment, row["document_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM semantic_jobs WHERE document_id = ?",
                (row["document_id"],),
            ).fetchone()
        return self._semantic_record(claimed)

    def renew_semantic_lease(self, document_id: str, lease_id: str, now: str) -> None:
        moment = to_iso(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE semantic_jobs SET claimed_at = ?, updated_at = ?
                WHERE document_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (moment, moment, document_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"semantic lease lost for {document_id!r}")

    def advance_semantic_job(
        self,
        document_id: str,
        lease_id: str,
        next_unit_index: int,
        now: str,
    ) -> None:
        if next_unit_index < 0:
            raise ValueError("next_unit_index must be non-negative")
        moment = to_iso(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE semantic_jobs SET next_unit_index = ?, updated_at = ?
                WHERE document_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (next_unit_index, moment, document_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"semantic lease lost for {document_id!r}")

    def defer_semantic_job(
        self,
        document_id: str,
        lease_id: str,
        error: str,
        now: str,
        retry_delay_seconds: int,
    ) -> None:
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        moment = to_iso(now)
        retry_at = to_iso(parse_iso(moment) + timedelta(seconds=retry_delay_seconds))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE semantic_jobs
                SET status = 'deferred', updated_at = ?, claimed_at = NULL,
                    lease_id = NULL, next_attempt_at = ?, last_error = ?
                WHERE document_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (moment, retry_at, error[:4000], document_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"semantic lease lost for {document_id!r}")

    def complete_semantic_job(self, document_id: str, lease_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM semantic_jobs
                WHERE document_id = ? AND status = 'processing' AND lease_id = ?
                """,
                (document_id, lease_id),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"semantic lease lost for {document_id!r}")

    def retry_failed(self, now: str) -> int:
        """Manually move all dead letters back to pending with fresh attempts."""
        moment = to_iso(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries
                SET status = 'pending', attempts = 0, updated_at = ?, claimed_at = NULL,
                    lease_id = NULL,
                    next_attempt_at = ?, completed_at = NULL, last_error = NULL
                WHERE status = 'failed'
                """,
                (moment, moment),
            )
            return cursor.rowcount
