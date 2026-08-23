"""Reliable worker that turns inbox deliveries into live graph state."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Literal, Protocol

from issue_graphrag.live.events import EventLog
from issue_graphrag.live.documents import text_units_for
from issue_graphrag.live.extraction import Extractor
from issue_graphrag.live.github_api import dependency_issue, pull_request_number
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.indexer import (
    NullExtractor,
    apply_event_deterministic,
    has_pending_extraction,
    pending_extraction_documents,
    publish_inferred_result,
    refresh_inferred,
)
from issue_graphrag.live.models import GraphDelta, LiveState, RepoEvent
from issue_graphrag.live.repositories import read_freshness, write_freshness
from issue_graphrag.live.store import read_state, write_state
from issue_graphrag.live.semantic_operations import SemanticBatchRunner
from issue_graphrag.live.timeutil import is_after, max_iso, next_iso, now_utc, to_iso


class GitHubReadClient(Protocol):
    def fetch_pull_request_files(self, repo: str, number: int) -> list[str]: ...

    def fetch_open_blocking_dependency_count(self, repo: str, number: int) -> int: ...


@dataclass(frozen=True)
class ProcessingResult:
    delivery_id: str
    status: Literal["succeeded", "deferred", "retrying", "failed"]
    delta: GraphDelta | None = None
    error: str | None = None
    work_type: Literal["delivery", "semantic"] = "delivery"
    document_id: str | None = None


class DeliveryWorker(Protocol):
    def process_one(self) -> ProcessingResult | None: ...


class _LeaseHeartbeat:
    def __init__(
        self,
        inbox: DeliveryInbox,
        delivery_id: str,
        lease_id: str,
        lease_seconds: int,
    ):
        self.inbox = inbox
        self.delivery_id = delivery_id
        self.lease_id = lease_id
        self.interval = max(0.1, lease_seconds / 3)
        self.stopped = Event()
        self.error: Exception | None = None
        self.thread = Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                self.inbox.renew_lease(
                    self.delivery_id,
                    self.lease_id,
                    to_iso(now_utc()),
                )
            except Exception as exc:
                self.error = exc
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self.thread.start()
        return self

    def check(self) -> None:
        if self.error is not None:
            raise RuntimeError("processing lease heartbeat failed") from self.error

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.stopped.set()
        self.thread.join(timeout=max(1.0, self.interval * 2))


class _SemanticLeaseHeartbeat:
    def __init__(
        self,
        inbox: DeliveryInbox,
        document_id: str,
        lease_id: str,
        lease_seconds: int,
    ):
        self.inbox = inbox
        self.document_id = document_id
        self.lease_id = lease_id
        self.interval = max(0.1, lease_seconds / 3)
        self.stopped = Event()
        self.error: Exception | None = None
        self.thread = Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                self.inbox.renew_semantic_lease(
                    self.document_id,
                    self.lease_id,
                    to_iso(now_utc()),
                )
            except Exception as exc:
                self.error = exc
                return

    def __enter__(self) -> "_SemanticLeaseHeartbeat":
        self.thread.start()
        return self

    def check(self) -> None:
        if self.error is not None:
            raise RuntimeError("semantic lease heartbeat failed") from self.error

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.stopped.set()
        self.thread.join(timeout=max(1.0, self.interval * 2))


class DeliveryProcessor:
    """Process one delivery lease at a time and close it only after durable output."""

    def __init__(
        self,
        repo: str,
        inbox: DeliveryInbox,
        state_path: Path,
        event_log: EventLog,
        extractor: Extractor,
        github: GitHubReadClient | None = None,
        hydrate_pull_request_files: bool = True,
        lease_seconds: int = 300,
        retry_delay_seconds: int = 30,
        max_attempts: int = 5,
        freshness_path: Path | None = None,
        semantic_runner: SemanticBatchRunner | None = None,
    ):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be non-negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.repo = repo
        self.inbox = inbox
        self.state_path = Path(state_path)
        self.event_log = event_log
        self.extractor = extractor
        self.github = github
        self.hydrate_pull_request_files = hydrate_pull_request_files
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.freshness_path = Path(freshness_path) if freshness_path is not None else None
        self.semantic_runner = semantic_runner

    def _record_freshness(
        self,
        *,
        state_commit_at: str | None,
        semantic_updated_at: str,
        error: str | None,
        semantic_pending: bool = False,
    ) -> None:
        if self.freshness_path is None:
            return
        freshness = read_freshness(self.freshness_path, self.repo)
        if state_commit_at is not None:
            freshness.last_state_commit_at = state_commit_at
        if error:
            freshness.semantic_status = "degraded"
        elif semantic_pending:
            # A later source commit must not erase an unresolved provider or
            # quota incident merely because the durable job is still pending.
            if freshness.semantic_status != "degraded":
                freshness.semantic_status = "pending"
        else:
            freshness.semantic_status = "current"
        freshness.semantic_updated_at = semantic_updated_at
        if error is not None or freshness.semantic_status != "degraded":
            freshness.last_error = error
        write_freshness(self.freshness_path, freshness)

    def _state(self) -> LiveState:
        if not self.state_path.exists():
            return LiveState(repo=self.repo)
        state = read_state(self.state_path)
        if state.repo != self.repo:
            raise ValueError(f"state belongs to {state.repo!r}, not {self.repo!r}")
        return state

    def _needs_hydration(self, event: RepoEvent, key: str) -> bool:
        """Whether this delivery still has to fetch ``key`` from GitHub.

        Hydration is durable replay input, not transient API state. A retry
        after the state commit but before the event-log append must reuse the
        exact observation that was already applied, even when GitHub has moved
        on in between. Re-reading would leave the audit log and the live state
        holding two different observations of one delivery, and replay would
        stop describing what the index actually did.

        Every attachment the worker fetches goes through this rule; today those
        are ``files`` and ``blocking_dependency_count``.
        """
        return key not in event.attachments

    def _hydrate(self, event: RepoEvent, state: LiveState) -> None:
        dependency = dependency_issue(event)
        if dependency is not None:
            dependency_repo, number = dependency
            if dependency_repo.casefold() != self.repo.casefold():
                return
            if not self._needs_hydration(event, "blocking_dependency_count"):
                return
            if self.github is None:
                raise RuntimeError("issue_dependencies processing requires a GitHub read client")
            event.attachments["blocking_dependency_count"] = (
                self.github.fetch_open_blocking_dependency_count(dependency_repo, number)
            )
            return

        number = pull_request_number(event)
        if (
            number is None
            or self.github is None
            or not self.hydrate_pull_request_files
            or not self._needs_hydration(event, "files")
        ):
            return
        known = state.items.get(f"{event.repo}#pull-{number}")
        if event.event_type == "issue_comment" and known is not None and known.files:
            return
        refresh_actions = {"opened", "edited", "reopened", "synchronize"}
        if (
            event.event_type == "pull_request"
            and known is not None
            and known.files
            and event.action not in refresh_actions
        ):
            return
        event.attachments["files"] = sorted(
            set(self.github.fetch_pull_request_files(event.repo, number))
        )

    def _queue_pending_semantics(self, state: LiveState, now: str) -> None:
        if isinstance(self.extractor, NullExtractor):
            return
        for document_id in pending_extraction_documents(state):
            item = state.items[document_id]
            existing = self.inbox.get_semantic_job(document_id)
            if existing is not None and existing.status == "processing":
                continue
            self.inbox.upsert_semantic_job(
                document_id=document_id,
                content_signature=item.extraction_signature(),
                trigger_delivery_id=item.source_delivery_id or "seed",
                total_units=len(text_units_for(item)),
                now=now,
            )

    @staticmethod
    def _semantic_moment(state: LiveState, candidate: str) -> str:
        moment = max_iso(state.last_event_at, candidate) or candidate
        if state.last_event_at and not is_after(moment, state.last_event_at):
            return next_iso(state.last_event_at)
        return moment

    def _process_one_semantic(self, started_at: str) -> ProcessingResult | None:
        if isinstance(self.extractor, NullExtractor):
            return None
        if self.state_path.exists():
            # A bootstrap written in deterministic-only mode has no source
            # delivery to enqueue its semantic backfill. Reconcile state into
            # durable jobs whenever the worker is otherwise idle.
            self._queue_pending_semantics(self._state(), started_at)
        job = self.inbox.claim_semantic_job(started_at, self.lease_seconds)
        if job is None:
            return None
        if job.lease_id is None:
            raise RuntimeError("claimed semantic job has no lease id")
        lease_id = job.lease_id

        try:
            with _SemanticLeaseHeartbeat(
                self.inbox,
                job.document_id,
                lease_id,
                self.lease_seconds,
            ) as heartbeat:
                state = self._state()
                item = state.items.get(job.document_id)
                if item is None:
                    heartbeat.check()
                    self.inbox.complete_semantic_job(job.document_id, lease_id)
                    return ProcessingResult(
                        job.trigger_delivery_id,
                        "succeeded",
                        work_type="semantic",
                        document_id=job.document_id,
                    )

                current_signature = item.extraction_signature()
                if state.extraction_signatures.get(job.document_id) == current_signature:
                    heartbeat.check()
                    self.inbox.complete_semantic_job(job.document_id, lease_id)
                    return ProcessingResult(
                        job.trigger_delivery_id,
                        "succeeded",
                        work_type="semantic",
                        document_id=job.document_id,
                    )

                if current_signature != job.content_signature:
                    heartbeat.check()
                    self.inbox.complete_semantic_job(job.document_id, lease_id)
                    self.inbox.upsert_semantic_job(
                        document_id=job.document_id,
                        content_signature=current_signature,
                        trigger_delivery_id=item.source_delivery_id
                        or job.trigger_delivery_id,
                        total_units=len(text_units_for(item)),
                        now=started_at,
                    )
                    return ProcessingResult(
                        job.trigger_delivery_id,
                        "deferred",
                        error="content changed while semantic work was pending",
                        work_type="semantic",
                        document_id=job.document_id,
                    )

                units = text_units_for(item)
                if self.semantic_runner is not None:
                    outcome = self.semantic_runner.run_batch(
                        content_signature=current_signature,
                        units=units,
                        next_unit_index=job.next_unit_index,
                        bootstrap=job.trigger_delivery_id == "seed",
                        now=started_at,
                        on_advance=lambda cursor: self.inbox.advance_semantic_job(
                            job.document_id,
                            lease_id,
                            cursor,
                            started_at,
                        ),
                    )
                    if not outcome.complete:
                        heartbeat.check()
                        reason = outcome.deferred_reason or "semantic batch deferred"
                        self.inbox.defer_semantic_job(
                            job.document_id,
                            lease_id,
                            reason,
                            now=started_at,
                            retry_delay_seconds=0,
                        )
                        freshness_error: str | None = None
                        try:
                            self._record_freshness(
                                state_commit_at=None,
                                semantic_updated_at=started_at,
                                error=None,
                                semantic_pending=True,
                            )
                        except Exception as exc:
                            freshness_error = (
                                "freshness update failed: "
                                f"{type(exc).__name__}: {exc}"
                            )
                        return ProcessingResult(
                            job.trigger_delivery_id,
                            "deferred",
                            error=freshness_error or reason,
                            work_type="semantic",
                            document_id=job.document_id,
                        )
                    if outcome.result is None:
                        raise RuntimeError("complete semantic batch has no extraction result")

                # Work on a copy so provider/validation failures and partial
                # cache never leak a partial reconcile into deterministic state.
                working = state.model_copy(deep=True)
                moment = self._semantic_moment(working, started_at)
                if self.semantic_runner is None:
                    refresh_inferred(
                        working,
                        self.extractor,
                        moment,
                        job.trigger_delivery_id,
                        document_ids=[job.document_id],
                    )
                else:
                    publish_inferred_result(
                        working,
                        job.document_id,
                        outcome.result,
                        moment,
                        job.trigger_delivery_id,
                    )
                working.last_event_at = moment
                heartbeat.check()
                write_state(self.state_path, working)
                heartbeat.check()

            self.inbox.complete_semantic_job(job.document_id, lease_id)
            error: str | None = None
            try:
                self._record_freshness(
                    state_commit_at=working.last_event_at,
                    semantic_updated_at=working.last_event_at or started_at,
                    error=None,
                    semantic_pending=has_pending_extraction(working),
                )
            except Exception as freshness_exc:
                error = (
                    "freshness update failed: "
                    f"{type(freshness_exc).__name__}: {freshness_exc}"
                )
            return ProcessingResult(
                job.trigger_delivery_id,
                "succeeded",
                error=error,
                work_type="semantic",
                document_id=job.document_id,
            )
        except Exception as exc:
            completed_at = started_at
            error = f"{type(exc).__name__}: {exc}"
            self.inbox.defer_semantic_job(
                job.document_id,
                lease_id,
                error,
                now=completed_at,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            try:
                self._record_freshness(
                    state_commit_at=None,
                    semantic_updated_at=completed_at,
                    error=error,
                    semantic_pending=True,
                )
            except Exception as freshness_exc:
                error += (
                    "; freshness update failed: "
                    f"{type(freshness_exc).__name__}: {freshness_exc}"
                )
            return ProcessingResult(
                job.trigger_delivery_id,
                "deferred",
                error=error,
                work_type="semantic",
                document_id=job.document_id,
            )

    def process_one(self, now: str | None = None) -> ProcessingResult | None:
        started_at = to_iso(now) if now else to_iso(now_utc())
        claimed = self.inbox.claim_next(
            now=started_at,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if claimed is None:
            return self._process_one_semantic(started_at)

        event = claimed.event
        if claimed.lease_id is None:
            raise RuntimeError("claimed delivery has no lease id")
        lease_id = claimed.lease_id
        try:
            with _LeaseHeartbeat(
                self.inbox,
                event.delivery_id,
                lease_id,
                self.lease_seconds,
            ) as heartbeat:
                if event.repo != self.repo:
                    raise ValueError(f"delivery belongs to {event.repo!r}, not {self.repo!r}")
                state = self._state()

                self._hydrate(event, state)
                self.inbox.update_event(event, lease_id=lease_id, now=started_at)

                # Phase 1 is the source-of-truth commit. It never calls the
                # extractor and is durable before semantic work becomes ready.
                delta = apply_event_deterministic(state, event)
                heartbeat.check()
                self.inbox.update_event(event, lease_id=lease_id, now=started_at)
                write_state(self.state_path, state)
                self.event_log.append_once(event)
                self._queue_pending_semantics(state, started_at)
                heartbeat.check()

            completed_at = to_iso(now) if now else to_iso(now_utc())
            self.inbox.mark_succeeded(event.delivery_id, lease_id, now=completed_at)
        except Exception as exc:
            completed_at = to_iso(now) if now else to_iso(now_utc())
            outcome = self.inbox.mark_failed(
                event.delivery_id,
                lease_id,
                f"{type(exc).__name__}: {exc}",
                now=completed_at,
                retry_delay_seconds=self.retry_delay_seconds,
                max_attempts=self.max_attempts,
            )
            error = f"{type(exc).__name__}: {exc}"
            try:
                self._record_freshness(
                    state_commit_at=None,
                    semantic_updated_at=completed_at,
                    error=error,
                )
            except Exception as freshness_exc:
                error += (
                    "; freshness update failed: "
                    f"{type(freshness_exc).__name__}: {freshness_exc}"
                )
            return ProcessingResult(event.delivery_id, outcome, error=error)

        error: str | None = None
        try:
            self._record_freshness(
                state_commit_at=event.indexed_at or completed_at,
                semantic_updated_at=completed_at,
                error=None,
                semantic_pending=has_pending_extraction(state),
            )
        except Exception as freshness_exc:
            error = (
                "freshness update failed: "
                f"{type(freshness_exc).__name__}: {freshness_exc}"
            )
        return ProcessingResult(event.delivery_id, "succeeded", delta=delta, error=error)


def run_worker_loop(
    processor: DeliveryWorker,
    poll_seconds: float,
    on_result: Callable[[ProcessingResult], None],
    on_error: Callable[[Exception], None],
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Supervise daemon iterations without swallowing an intentional shutdown.

    ``process_one`` normally turns delivery failures into retry/dead-letter
    results. Infrastructure can still fail while claiming a lease or recording
    that result. Those exceptions must not terminate a long-running worker.
    """
    if poll_seconds < 0:
        raise ValueError("poll_seconds must be non-negative")

    while True:
        try:
            result = processor.process_one()
        except Exception as exc:
            on_error(exc)
            sleep(max(1.0, poll_seconds))
            continue
        if result is None:
            sleep(poll_seconds)
        else:
            on_result(result)
