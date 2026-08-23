"""Persistent cache, quota accounting, and resumable LLM extraction batches."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from issue_graphrag.indexing.extractor import (
    EXTRACTION_RESPONSE_SCHEMA_SHA256,
    extraction_prompt,
)
from issue_graphrag.live.documents import LIVE_CHUNK_MAX_CHARS, LIVE_CHUNK_OVERLAP
from issue_graphrag.live.extraction import (
    ExtractionValidationError,
    EXTRACTION_REQUIRE_PARAMETERS,
    EXTRACTION_SCHEMA_NAME,
    LLMExtractor,
    UnitExtraction,
)
from issue_graphrag.live.timeutil import parse_iso, to_iso
from issue_graphrag.llm.client import CompletionMetadata
from issue_graphrag.models import ExtractionResult, TextUnit
from issue_graphrag.prompts import (
    ENTITY_EXTRACTION_PROMPT_SHA256,
    EXTRACTION_PROMPT_VERSION,
    EXTRACTION_SCHEMA_VERSION,
    assert_extraction_prompt_identity,
)


@dataclass(frozen=True)
class ExtractionIdentity:
    content_signature: str
    gateway: str
    requested_model: str
    prompt_version: str = EXTRACTION_PROMPT_VERSION
    prompt_sha256: str = ENTITY_EXTRACTION_PROMPT_SHA256
    extraction_schema_version: str = EXTRACTION_SCHEMA_VERSION
    extraction_schema_sha256: str = EXTRACTION_RESPONSE_SCHEMA_SHA256
    schema_name: str = EXTRACTION_SCHEMA_NAME
    strict_json_schema: bool = True
    max_output_tokens: int = 800
    chunk_max_chars: int = LIVE_CHUNK_MAX_CHARS
    chunk_overlap: int = LIVE_CHUNK_OVERLAP
    temperature: float = 0.0
    require_parameters: bool = EXTRACTION_REQUIRE_PARAMETERS

    def __post_init__(self) -> None:
        if self.max_output_tokens <= 0:
            raise ValueError("max output tokens must be positive")
        if self.chunk_max_chars <= self.chunk_overlap or self.chunk_overlap < 0:
            raise ValueError("chunk identity requires max chars greater than non-negative overlap")

    @property
    def namespace_key(self) -> str:
        """Identity of semantic production inputs other than document content."""
        return self._key(include_content=False)

    @property
    def cache_key(self) -> str:
        return self._key(include_content=True)

    def _key(self, *, include_content: bool) -> str:
        payload = json.dumps(
            {
                **(
                    {"content_signature": self.content_signature}
                    if include_content
                    else {}
                ),
                "gateway": self.gateway,
                "requested_model": self.requested_model,
                "prompt_version": self.prompt_version,
                "prompt_sha256": self.prompt_sha256,
                "extraction_schema_version": self.extraction_schema_version,
                "extraction_schema_sha256": self.extraction_schema_sha256,
                "schema_name": self.schema_name,
                "strict_json_schema": self.strict_json_schema,
                "max_output_tokens": self.max_output_tokens,
                "chunk_max_chars": self.chunk_max_chars,
                "chunk_overlap": self.chunk_overlap,
                "temperature": self.temperature,
                "require_parameters": self.require_parameters,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedExtraction:
    result: ExtractionResult
    metadata: CompletionMetadata
    created_at: str


class ExtractionCache:
    """Repo-local, append-only cache of validated per-TextUnit results."""

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
                CREATE TABLE IF NOT EXISTS extraction_results (
                    cache_key TEXT NOT NULL,
                    unit_index INTEGER NOT NULL,
                    unit_id TEXT NOT NULL,
                    unit_sha256 TEXT NOT NULL,
                    content_signature TEXT NOT NULL,
                    gateway TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    extraction_schema_version TEXT NOT NULL,
                    extraction_schema_sha256 TEXT NOT NULL DEFAULT '',
                    schema_name TEXT NOT NULL DEFAULT '',
                    strict_json_schema INTEGER NOT NULL DEFAULT 1,
                    max_output_tokens INTEGER NOT NULL DEFAULT 800,
                    chunk_max_chars INTEGER NOT NULL DEFAULT 2500,
                    chunk_overlap INTEGER NOT NULL DEFAULT 250,
                    temperature REAL NOT NULL DEFAULT 0,
                    require_parameters INTEGER NOT NULL DEFAULT 1,
                    result_json TEXT NOT NULL,
                    requested_model TEXT NOT NULL,
                    actual_model TEXT,
                    provider TEXT,
                    generation_id TEXT,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    usage_is_complete INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cache_key, unit_index)
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(extraction_results)"
                ).fetchall()
            }
            migrations = (
                ("extraction_schema_sha256", "TEXT NOT NULL DEFAULT ''"),
                ("schema_name", "TEXT NOT NULL DEFAULT ''"),
                ("strict_json_schema", "INTEGER NOT NULL DEFAULT 1"),
                ("max_output_tokens", "INTEGER NOT NULL DEFAULT 800"),
                ("chunk_max_chars", "INTEGER NOT NULL DEFAULT 2500"),
                ("chunk_overlap", "INTEGER NOT NULL DEFAULT 250"),
                ("temperature", "REAL NOT NULL DEFAULT 0"),
                ("require_parameters", "INTEGER NOT NULL DEFAULT 1"),
                ("usage_is_complete", "INTEGER NOT NULL DEFAULT 1"),
            )
            for column, declaration in migrations:
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE extraction_results ADD COLUMN {column} {declaration}"
                    )

    @staticmethod
    def _unit_hash(unit: TextUnit) -> str:
        return hashlib.sha256(unit.text.encode("utf-8")).hexdigest()

    def get(self, identity: ExtractionIdentity, unit: TextUnit) -> CachedExtraction | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM extraction_results
                WHERE cache_key = ? AND unit_index = ?""",
                (identity.cache_key, unit.order),
            ).fetchone()
        if row is None:
            return None
        if row["unit_id"] != unit.id or row["unit_sha256"] != self._unit_hash(unit):
            return None
        return CachedExtraction(
            result=ExtractionResult.model_validate_json(row["result_json"]),
            metadata=CompletionMetadata(
                requested_model=row["requested_model"],
                actual_model=row["actual_model"],
                provider=row["provider"],
                generation_id=row["generation_id"],
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                cost_usd=float(row["cost_usd"]),
                usage_is_complete=bool(row["usage_is_complete"]),
            ),
            created_at=row["created_at"],
        )

    def put(
        self,
        identity: ExtractionIdentity,
        unit: TextUnit,
        extraction: UnitExtraction,
        created_at: str,
    ) -> CachedExtraction:
        """Keep the first successful observation for a lookup identity."""
        metadata = extraction.metadata
        if metadata.requested_model != identity.requested_model:
            raise ValueError("completion requested-model metadata does not match cache identity")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO extraction_results (
                    cache_key, unit_index, unit_id, unit_sha256,
                    content_signature, gateway, prompt_version, prompt_sha256,
                    extraction_schema_version, extraction_schema_sha256,
                    schema_name, strict_json_schema, max_output_tokens,
                    chunk_max_chars, chunk_overlap, temperature,
                    require_parameters, result_json,
                    requested_model, actual_model, provider, generation_id,
                    input_tokens, output_tokens, cost_usd, usage_is_complete, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.cache_key,
                    unit.order,
                    unit.id,
                    self._unit_hash(unit),
                    identity.content_signature,
                    identity.gateway,
                    identity.prompt_version,
                    identity.prompt_sha256,
                    identity.extraction_schema_version,
                    identity.extraction_schema_sha256,
                    identity.schema_name,
                    int(identity.strict_json_schema),
                    identity.max_output_tokens,
                    identity.chunk_max_chars,
                    identity.chunk_overlap,
                    identity.temperature,
                    int(identity.require_parameters),
                    extraction.result.model_dump_json(),
                    metadata.requested_model,
                    metadata.actual_model,
                    metadata.provider,
                    metadata.generation_id,
                    metadata.input_tokens,
                    metadata.output_tokens,
                    metadata.cost_usd,
                    int(metadata.usage_is_complete),
                    to_iso(created_at),
                ),
            )
        cached = self.get(identity, unit)
        if cached is None:
            raise RuntimeError("validated extraction cache write was not durable")
        return cached

    def complete_result(
        self,
        identity: ExtractionIdentity,
        units: list[TextUnit],
    ) -> ExtractionResult | None:
        entities = []
        relationships = []
        for unit in units:
            cached = self.get(identity, unit)
            if cached is None:
                return None
            entities.extend(cached.result.entities)
            relationships.extend(cached.result.relationships)
        return ExtractionResult(entities=entities, relationships=relationships)

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM extraction_results").fetchone()[0])


@dataclass(frozen=True)
class QuotaPolicy:
    daily_calls: int = 250
    daily_input_tokens: int = 300_000
    daily_output_tokens: int = 125_000
    monthly_cost_usd: float = 3.0
    bootstrap_calls: int = 200
    bootstrap_input_tokens: int = 250_000
    bootstrap_output_tokens: int = 100_000
    input_price_per_million_usd: float = 0.25
    output_price_per_million_usd: float = 1.50
    cost_safety_multiplier: float = 2.0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.__dict__.values()):
            raise ValueError("quota limits and prices must be non-negative")
        if (
            self.input_price_per_million_usd <= 0
            or self.output_price_per_million_usd <= 0
            or self.cost_safety_multiplier <= 0
        ):
            raise ValueError("quota prices and cost safety multiplier must be positive")

    def reserved_cost(self, input_tokens: int, output_tokens: int) -> float:
        estimated = (
            input_tokens * self.input_price_per_million_usd
            + output_tokens * self.output_price_per_million_usd
        ) / 1_000_000
        return estimated * self.cost_safety_multiplier


class QuotaExceeded(RuntimeError):
    """A provider request was rejected before dispatch by a durable hard cap."""


@dataclass(frozen=True)
class QuotaReservation:
    reservation_id: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_usd: float


@dataclass(frozen=True)
class UsageSummary:
    utc_day: str
    daily_calls: int
    daily_input_tokens: int
    daily_output_tokens: int
    utc_month: str
    monthly_cost_usd: float
    request_states: dict[str, int]


class QuotaLedger:
    """Cross-repository atomic request reservations and actual usage."""

    def __init__(
        self,
        path: Path,
        policy: QuotaPolicy | None = None,
        reservation_lease_seconds: int = 300,
    ):
        if reservation_lease_seconds <= 0:
            raise ValueError("reservation lease seconds must be positive")
        self.path = Path(path)
        self.policy = policy or QuotaPolicy()
        self.reservation_lease_seconds = reservation_lease_seconds
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
                CREATE TABLE IF NOT EXISTS llm_requests (
                    reservation_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    unit_index INTEGER NOT NULL,
                    utc_day TEXT NOT NULL,
                    utc_month TEXT NOT NULL,
                    bootstrap INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('reserved', 'completed', 'unknown')),
                    reserved_input_tokens INTEGER NOT NULL,
                    reserved_output_tokens INTEGER NOT NULL,
                    reserved_cost_usd REAL NOT NULL,
                    actual_input_tokens INTEGER,
                    actual_output_tokens INTEGER,
                    actual_cost_usd REAL,
                    actual_model TEXT,
                    provider TEXT,
                    generation_id TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT,
                    dispatched_at TEXT,
                    released_at TEXT,
                    usage_is_complete INTEGER
                );
                CREATE INDEX IF NOT EXISTS llm_requests_day ON llm_requests(utc_day);
                CREATE INDEX IF NOT EXISTS llm_requests_month ON llm_requests(utc_month);
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(llm_requests)").fetchall()
            }
            for column, declaration in (
                ("dispatched_at", "TEXT"),
                ("released_at", "TEXT"),
                ("usage_is_complete", "INTEGER"),
            ):
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE llm_requests ADD COLUMN {column} {declaration}"
                    )

    @staticmethod
    def _effective(column: str, reserved: str) -> str:
        return (
            "CASE WHEN released_at IS NOT NULL THEN 0 "
            f"WHEN status = 'completed' THEN COALESCE({column}, {reserved}) "
            f"ELSE {reserved} END"
        )

    @staticmethod
    def _effective_calls() -> str:
        return "CASE WHEN released_at IS NULL THEN 1 ELSE 0 END"

    def _usage(self, connection: sqlite3.Connection, where: str, value: str):  # noqa: ANN201
        return connection.execute(
            f"""
            SELECT COALESCE(SUM({self._effective_calls()}), 0) AS calls,
                   COALESCE(SUM({self._effective('actual_input_tokens', 'reserved_input_tokens')}), 0) AS input_tokens,
                   COALESCE(SUM({self._effective('actual_output_tokens', 'reserved_output_tokens')}), 0) AS output_tokens,
                   COALESCE(SUM({self._effective('actual_cost_usd', 'reserved_cost_usd')}), 0) AS cost_usd
            FROM llm_requests WHERE {where} = ?
            """,
            (value,),
        ).fetchone()

    def _reconcile_orphans(
        self,
        connection: sqlite3.Connection,
        now: str,
    ) -> dict[str, int]:
        """Release provably undispatched work; retain dispatched work conservatively."""
        moment = to_iso(now)
        stale_before = to_iso(
            parse_iso(moment) - timedelta(seconds=self.reservation_lease_seconds)
        )
        released = connection.execute(
            """
            UPDATE llm_requests
            SET released_at = ?, completed_at = ?,
                last_error = 'reservation lease expired before provider dispatch'
            WHERE status = 'reserved' AND dispatched_at IS NULL
              AND released_at IS NULL AND created_at <= ?
            """,
            (moment, moment, stale_before),
        )
        unknown = connection.execute(
            """
            UPDATE llm_requests
            SET status = 'unknown', completed_at = ?,
                last_error = 'provider outcome unknown after reservation lease expired'
            WHERE status = 'reserved' AND dispatched_at IS NOT NULL
              AND released_at IS NULL AND dispatched_at <= ?
            """,
            (moment, stale_before),
        )
        return {"released": released.rowcount, "unknown": unknown.rowcount}

    def reconcile_orphans(self, now: str) -> dict[str, int]:
        """Persist expired-reservation state independently of later admission."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._reconcile_orphans(connection, now)

    def reserve(
        self,
        *,
        repo: str,
        identity: ExtractionIdentity,
        unit: TextUnit,
        estimated_input_tokens: int,
        max_output_tokens: int,
        bootstrap: bool,
        now: str,
    ) -> QuotaReservation:
        if estimated_input_tokens < 0 or max_output_tokens < 0:
            raise ValueError("token reservations must be non-negative")
        moment = parse_iso(to_iso(now))
        day = moment.strftime("%Y-%m-%d")
        month = moment.strftime("%Y-%m")
        cost = self.policy.reserved_cost(estimated_input_tokens, max_output_tokens)
        # Reconciliation owns its commit. If admission below raises, operators
        # must still see why expired work is released or conservatively unknown.
        # Another worker may reserve between these transactions; the admission
        # transaction re-reads all usage, so the hard-cap decision stays atomic.
        self.reconcile_orphans(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            daily = self._usage(connection, "utc_day", day)
            monthly = self._usage(connection, "utc_month", month)
            checks = [
                ("daily calls", int(daily["calls"]) + 1, self.policy.daily_calls),
                (
                    "daily input tokens",
                    int(daily["input_tokens"]) + estimated_input_tokens,
                    self.policy.daily_input_tokens,
                ),
                (
                    "daily output tokens",
                    int(daily["output_tokens"]) + max_output_tokens,
                    self.policy.daily_output_tokens,
                ),
                (
                    "monthly cost",
                    float(monthly["cost_usd"]) + cost,
                    self.policy.monthly_cost_usd,
                ),
            ]
            if bootstrap:
                bootstrap_usage = connection.execute(
                    f"""
                    SELECT COALESCE(SUM({self._effective_calls()}), 0) AS calls,
                           COALESCE(SUM({self._effective('actual_input_tokens', 'reserved_input_tokens')}), 0) AS input_tokens,
                           COALESCE(SUM({self._effective('actual_output_tokens', 'reserved_output_tokens')}), 0) AS output_tokens
                    FROM llm_requests WHERE bootstrap = 1
                    """
                ).fetchone()
                checks.extend(
                    [
                        (
                            "bootstrap calls",
                            int(bootstrap_usage["calls"]) + 1,
                            self.policy.bootstrap_calls,
                        ),
                        (
                            "bootstrap input tokens",
                            int(bootstrap_usage["input_tokens"]) + estimated_input_tokens,
                            self.policy.bootstrap_input_tokens,
                        ),
                        (
                            "bootstrap output tokens",
                            int(bootstrap_usage["output_tokens"]) + max_output_tokens,
                            self.policy.bootstrap_output_tokens,
                        ),
                    ]
                )
            for label, proposed, limit in checks:
                if proposed > limit:
                    raise QuotaExceeded(f"{label} quota exhausted ({proposed} > {limit})")

            reservation_id = uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO llm_requests (
                    reservation_id, repo, cache_key, unit_index, utc_day, utc_month,
                    bootstrap, status, reserved_input_tokens, reserved_output_tokens,
                    reserved_cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    repo,
                    identity.cache_key,
                    unit.order,
                    day,
                    month,
                    int(bootstrap),
                    estimated_input_tokens,
                    max_output_tokens,
                    cost,
                    to_iso(now),
                ),
            )
        return QuotaReservation(reservation_id, estimated_input_tokens, max_output_tokens, cost)

    def mark_dispatched(self, reservation: QuotaReservation, now: str) -> None:
        """Durably cross the money boundary before any provider call can start."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE llm_requests SET dispatched_at = ?
                WHERE reservation_id = ? AND status = 'reserved'
                  AND dispatched_at IS NULL AND released_at IS NULL
                """,
                (to_iso(now), reservation.reservation_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("quota reservation cannot be dispatched")

    def settle(self, reservation: QuotaReservation, metadata: CompletionMetadata, now: str) -> None:
        if metadata.usage_is_complete:
            input_tokens = metadata.input_tokens
            output_tokens = metadata.output_tokens
            cost_usd = metadata.cost_usd
        else:
            # OpenRouter documents usage on every non-streaming response. If
            # that contract is ever incomplete, never turn missing money or
            # token fields into zero: retain the conservative preflight values.
            input_tokens = max(metadata.input_tokens, reservation.reserved_input_tokens)
            output_tokens = max(metadata.output_tokens, reservation.reserved_output_tokens)
            cost_usd = max(metadata.cost_usd, reservation.reserved_cost_usd)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE llm_requests
                SET status = 'completed', actual_input_tokens = ?, actual_output_tokens = ?,
                    actual_cost_usd = ?, actual_model = ?, provider = ?, generation_id = ?,
                    completed_at = ?, last_error = NULL, usage_is_complete = ?
                WHERE reservation_id = ? AND status = 'reserved'
                  AND dispatched_at IS NOT NULL AND released_at IS NULL
                """,
                (
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    metadata.actual_model,
                    metadata.provider,
                    metadata.generation_id,
                    to_iso(now),
                    int(metadata.usage_is_complete),
                    reservation.reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("quota reservation was already finalized")

    def mark_unknown(self, reservation: QuotaReservation, error: Exception, now: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE llm_requests
                SET status = 'unknown', completed_at = ?, last_error = ?
                WHERE reservation_id = ? AND status = 'reserved'
                  AND released_at IS NULL
                """,
                (to_iso(now), f"{type(error).__name__}: {error}"[:4000], reservation.reservation_id),
            )

    @staticmethod
    def _counts(connection: sqlite3.Connection) -> dict[str, int]:
        rows = connection.execute(
            """
            SELECT CASE WHEN released_at IS NOT NULL THEN 'released' ELSE status END
                       AS effective_status,
                   COUNT(*) AS count
            FROM llm_requests GROUP BY effective_status
            """
        ).fetchall()
        return {row["effective_status"]: int(row["count"]) for row in rows}

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            return self._counts(connection)

    def usage_summary(self, now: str) -> UsageSummary:
        # Status is also the operator reconciliation path: it persists terminal
        # orphan labels before reporting effective usage and request states.
        self.reconcile_orphans(now)
        moment = parse_iso(to_iso(now))
        day = moment.strftime("%Y-%m-%d")
        month = moment.strftime("%Y-%m")
        with self._connect() as connection:
            connection.execute("BEGIN")
            daily = self._usage(connection, "utc_day", day)
            monthly = self._usage(connection, "utc_month", month)
            request_states = self._counts(connection)
        return UsageSummary(
            utc_day=day,
            daily_calls=int(daily["calls"]),
            daily_input_tokens=int(daily["input_tokens"]),
            daily_output_tokens=int(daily["output_tokens"]),
            utc_month=month,
            monthly_cost_usd=float(monthly["cost_usd"]),
            request_states=request_states,
        )


@dataclass(frozen=True)
class BatchPolicy:
    max_calls: int = 12
    max_input_tokens: int = 20_000
    max_output_tokens: int = 10_000
    max_output_tokens_per_call: int = 800

    def __post_init__(self) -> None:
        if min(self.__dict__.values()) <= 0:
            raise ValueError("semantic batch limits must be positive")
        if self.max_output_tokens_per_call > self.max_output_tokens:
            raise ValueError("per-call output limit cannot exceed the semantic batch limit")


@dataclass(frozen=True)
class BatchOutcome:
    next_unit_index: int
    complete: bool
    result: ExtractionResult | None
    provider_calls: int
    cache_hits: int
    input_tokens: int
    output_tokens: int
    deferred_reason: str | None = None


class SemanticBatchRunner:
    def __init__(
        self,
        *,
        repo: str,
        extractor: LLMExtractor,
        cache: ExtractionCache,
        quota: QuotaLedger,
        gateway: str = "openrouter",
        batch_policy: BatchPolicy | None = None,
    ):
        assert_extraction_prompt_identity()
        self.repo = repo
        self.extractor = extractor
        self.cache = cache
        self.quota = quota
        self.gateway = gateway
        self.batch_policy = batch_policy or BatchPolicy()

    def identity_for(self, content_signature: str) -> ExtractionIdentity:
        return ExtractionIdentity(
            content_signature=content_signature,
            gateway=self.gateway,
            requested_model=self.extractor.requested_model,
            max_output_tokens=self.batch_policy.max_output_tokens_per_call,
            chunk_max_chars=LIVE_CHUNK_MAX_CHARS,
            chunk_overlap=LIVE_CHUNK_OVERLAP,
        )

    @property
    def semantic_namespace(self) -> str:
        return self.identity_for("").namespace_key

    @staticmethod
    def estimate_input_tokens(unit: TextUnit) -> int:
        # Admission control must run before provider accounting exists. UTF-8
        # bytes plus protocol slack is conservative, then actual usage replaces it.
        return len(extraction_prompt(unit).encode("utf-8")) + 256

    def run_batch(
        self,
        *,
        content_signature: str,
        units: list[TextUnit],
        next_unit_index: int,
        bootstrap: bool,
        now: str,
        on_advance=None,  # noqa: ANN001
    ) -> BatchOutcome:
        identity = self.identity_for(content_signature)
        next_unit_index = min(max(0, next_unit_index), len(units))
        # The cursor is a scheduling hint. The cache is durable truth after a
        # crash between provider completion and cursor advancement.
        for index in range(min(next_unit_index, len(units))):
            if self.cache.get(identity, units[index]) is None:
                next_unit_index = index
                break

        calls = cache_hits = input_tokens = output_tokens = 0
        while next_unit_index < len(units):
            unit = units[next_unit_index]
            cached = self.cache.get(identity, unit)
            if cached is not None:
                cache_hits += 1
                next_unit_index += 1
                if on_advance is not None:
                    on_advance(next_unit_index)
                continue

            estimated_input = self.estimate_input_tokens(unit)
            limits = self.batch_policy
            if (
                calls + 1 > limits.max_calls
                or input_tokens + estimated_input > limits.max_input_tokens
                or output_tokens + limits.max_output_tokens_per_call > limits.max_output_tokens
            ):
                return BatchOutcome(
                    next_unit_index,
                    False,
                    None,
                    calls,
                    cache_hits,
                    input_tokens,
                    output_tokens,
                    "semantic batch limit reached",
                )

            reservation = self.quota.reserve(
                repo=self.repo,
                identity=identity,
                unit=unit,
                estimated_input_tokens=estimated_input,
                max_output_tokens=limits.max_output_tokens_per_call,
                bootstrap=bootstrap,
                now=now,
            )
            self.quota.mark_dispatched(reservation, now)
            try:
                extraction = self.extractor.extract_unit(
                    unit,
                    max_output_tokens=limits.max_output_tokens_per_call,
                )
            except ExtractionValidationError as exc:
                self.quota.settle(reservation, exc.metadata, now)
                raise
            except Exception as exc:
                self.quota.mark_unknown(reservation, exc, now)
                raise
            cache_error: Exception | None = None
            try:
                self.cache.put(identity, unit, extraction, now)
            except Exception as exc:
                cache_error = exc
            self.quota.settle(reservation, extraction.metadata, now)
            if cache_error is not None:
                raise cache_error

            calls += 1
            input_tokens += extraction.metadata.input_tokens
            output_tokens += extraction.metadata.output_tokens
            next_unit_index += 1
            if on_advance is not None:
                on_advance(next_unit_index)

        result = self.cache.complete_result(identity, units)
        if result is None:
            raise RuntimeError("semantic cursor completed without a complete validated cache")
        return BatchOutcome(
            next_unit_index,
            True,
            result,
            calls,
            cache_hits,
            input_tokens,
            output_tokens,
        )
