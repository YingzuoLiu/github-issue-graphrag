from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from issue_graphrag.live.models import RepoEvent
from issue_graphrag.live.timeutil import max_iso, now_utc, to_iso

DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"


def _header(envelope: dict[str, Any], name: str) -> str | None:
    headers = envelope.get("headers") or {}
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return str(value)
    return None


def _payload_timestamp(payload: dict[str, Any]) -> str | None:
    """Pick the most recent timestamp the payload itself states."""
    candidates: list[str | None] = []
    for key in ("comment", "pull_request", "issue"):
        section = payload.get(key) or {}
        if isinstance(section, dict):
            candidates.extend(
                [section.get("updated_at"), section.get("created_at"), section.get("closed_at")]
            )
    return max_iso(*candidates)


def normalize_envelope(envelope: dict[str, Any], default_repo: str | None = None) -> RepoEvent:
    """Turn a stored or received delivery envelope into a ``RepoEvent``.

    Replay determinism depends on the timestamp being derived from the event
    itself. The wall clock is only a last resort for live deliveries that carry
    no usable timestamp at all.
    """
    payload = envelope.get("payload") or {}
    delivery_id = envelope.get("delivery_id") or _header(envelope, DELIVERY_HEADER)
    event_type = envelope.get("event_type") or _header(envelope, EVENT_HEADER)

    if not delivery_id:
        raise ValueError("delivery envelope is missing a delivery id")
    if not event_type:
        raise ValueError("delivery envelope is missing an event type")

    repository = payload.get("repository") or {}
    repo = envelope.get("repo") or repository.get("full_name") or default_repo
    if not repo:
        raise ValueError("delivery envelope is missing a repository")

    received_at = envelope.get("received_at") or _payload_timestamp(payload)

    return RepoEvent(
        delivery_id=str(delivery_id),
        event_type=str(event_type),
        action=str(payload.get("action") or envelope.get("action") or ""),
        repo=str(repo),
        received_at=to_iso(received_at) if received_at else to_iso(now_utc()),
        payload=payload,
        attachments=envelope.get("attachments") or {},
    )


def load_events(path: Path, default_repo: str | None = None) -> list[RepoEvent]:
    """Load one envelope file, a JSON list of envelopes, or a directory of them.

    Directory entries are replayed in filename order, which is why the shipped
    fixtures are numbered.
    """
    if path.is_dir():
        events: list[RepoEvent] = []
        for child in sorted(path.glob("*.json")):
            events.extend(load_events(child, default_repo=default_repo))
        return events

    with path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    envelopes = raw if isinstance(raw, list) else [raw]
    return [normalize_envelope(envelope, default_repo=default_repo) for envelope in envelopes]


class EventLog:
    """Append-only JSONL record of every delivery this index has seen."""

    def __init__(self, path: Path):
        self.path = path

    def _repair_truncated_tail(self) -> None:
        """Drop only an incomplete final JSONL record left by process death."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with self.path.open("r+b") as handle:
            raw = handle.read()
            if raw.endswith(b"\n"):
                return
            last_complete = raw.rfind(b"\n")
            handle.truncate(last_complete + 1)
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, event: RepoEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._repair_truncated_tail()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append_once(self, event: RepoEvent) -> bool:
        """Append unless this delivery is already present in the audit log."""
        if event.delivery_id in self.delivery_ids():
            return False
        self.append(event)
        return True

    def read_all(self) -> list[RepoEvent]:
        if not self.path.exists():
            return []
        self._repair_truncated_tail()
        events: list[RepoEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    events.append(RepoEvent.model_validate(json.loads(line)))
        return events

    def delivery_ids(self) -> set[str]:
        return {event.delivery_id for event in self.read_all()}

    def extend(self, events: Iterable[RepoEvent]) -> None:
        for event in events:
            self.append(event)
