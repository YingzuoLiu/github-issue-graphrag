"""Reliable worker that turns inbox deliveries into live graph state."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Literal, Protocol

from issue_graphrag.live.events import EventLog
from issue_graphrag.live.extraction import Extractor
from issue_graphrag.live.github_api import pull_request_number
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.indexer import apply_event
from issue_graphrag.live.models import GraphDelta, LiveState, RepoEvent
from issue_graphrag.live.store import read_state, write_state
from issue_graphrag.live.timeutil import now_utc, to_iso


class PullFileClient(Protocol):
    def fetch_pull_request_files(self, repo: str, number: int) -> list[str]: ...


@dataclass(frozen=True)
class ProcessingResult:
    delivery_id: str
    status: Literal["succeeded", "retrying", "failed"]
    delta: GraphDelta | None = None
    error: str | None = None


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


class DeliveryProcessor:
    """Process one delivery lease at a time and close it only after durable output."""

    def __init__(
        self,
        repo: str,
        inbox: DeliveryInbox,
        state_path: Path,
        event_log: EventLog,
        extractor: Extractor,
        github: PullFileClient | None = None,
        lease_seconds: int = 300,
        retry_delay_seconds: int = 30,
        max_attempts: int = 5,
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
        self.lease_seconds = lease_seconds
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts

    def _state(self) -> LiveState:
        if not self.state_path.exists():
            return LiveState(repo=self.repo)
        state = read_state(self.state_path)
        if state.repo != self.repo:
            raise ValueError(f"state belongs to {state.repo!r}, not {self.repo!r}")
        return state

    def _hydrate(self, event: RepoEvent, state: LiveState) -> None:
        number = pull_request_number(event)
        if number is None or self.github is None:
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

    def process_one(self, now: str | None = None) -> ProcessingResult | None:
        started_at = to_iso(now) if now else to_iso(now_utc())
        claimed = self.inbox.claim_next(
            now=started_at,
            lease_seconds=self.lease_seconds,
            max_attempts=self.max_attempts,
        )
        if claimed is None:
            return None

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
                # Enrichment is part of the replay input, not transient API state.
                self.inbox.update_event(event, lease_id=lease_id, now=started_at)

                delta = apply_event(state, event, self.extractor)
                heartbeat.check()
                # Persist the logical index time before the state. If the process
                # dies after state replacement, a retry can append the exact event.
                self.inbox.update_event(event, lease_id=lease_id, now=started_at)
                write_state(self.state_path, state)
                self.event_log.append_once(event)
                heartbeat.check()
            completed_at = to_iso(now) if now else to_iso(now_utc())
            self.inbox.mark_succeeded(event.delivery_id, lease_id, now=completed_at)
            return ProcessingResult(event.delivery_id, "succeeded", delta=delta)
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
            return ProcessingResult(
                event.delivery_id,
                outcome,
                error=f"{type(exc).__name__}: {exc}",
            )


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
