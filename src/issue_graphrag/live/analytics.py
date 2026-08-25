"""Minimal, anonymous product analytics for the Contribution Radar.

This database is deliberately separate from repository state and the ingestion
single-writer lane.  Its API accepts only the fields permitted by FR-RADAR-08;
callers cannot accidentally pass titles, bodies, URLs or GitHub identities.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from issue_graphrag.live.repositories import canonical_repo
from issue_graphrag.live.timeutil import now_utc, to_iso

LOGGER = logging.getLogger(__name__)

RADAR_ANALYTICS_EVENTS = (
    "repo_selected",
    "radar_viewed",
    "opportunity_opened",
    "evidence_opened",
    "github_opened",
)
RADAR_ANALYTICS_SOURCES = {
    "repo_selected": frozenset({"repo_selector"}),
    "radar_viewed": frozenset({"radar_page"}),
    "opportunity_opened": frozenset({"radar_card", "recently_changed"}),
    "evidence_opened": frozenset({"issue_detail"}),
    "github_opened": frozenset({"radar_card", "issue_detail"}),
}
_ANONYMOUS_SESSION = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class RadarAnalytics:
    """Append-only SQLite event sink with a closed, privacy-minimal schema."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def record(
        self,
        *,
        event_name: str,
        anonymous_session: str,
        repo: str,
        issue_number: int | None,
        ui_source: str,
        occurred_at: str | None = None,
    ) -> None:
        if event_name not in RADAR_ANALYTICS_EVENTS:
            raise ValueError(f"unsupported radar analytics event: {event_name}")
        if not _ANONYMOUS_SESSION.fullmatch(anonymous_session):
            raise ValueError("anonymous_session must be a random URL-safe token")
        if issue_number is not None and issue_number <= 0:
            raise ValueError("issue_number must be positive when present")
        if ui_source not in RADAR_ANALYTICS_SOURCES[event_name]:
            raise ValueError(
                f"unsupported UI source {ui_source!r} for radar event {event_name!r}"
            )

        normalized = canonical_repo(repo)
        timestamp = to_iso(occurred_at) if occurred_at else to_iso(now_utc())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS radar_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_name TEXT NOT NULL CHECK (
                        event_name IN (
                            'repo_selected',
                            'radar_viewed',
                            'opportunity_opened',
                            'evidence_opened',
                            'github_opened'
                        )
                    ),
                    anonymous_session TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    issue_number INTEGER,
                    occurred_at TEXT NOT NULL,
                    ui_source TEXT NOT NULL CHECK (
                        ui_source IN (
                            'repo_selector',
                            'radar_page',
                            'radar_card',
                            'recently_changed',
                            'issue_detail'
                        )
                    )
                )
                """
            )
            connection.execute(
                """
                INSERT INTO radar_events (
                    event_name,
                    anonymous_session,
                    repo,
                    issue_number,
                    occurred_at,
                    ui_source
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_name,
                    anonymous_session,
                    normalized,
                    issue_number,
                    timestamp,
                    ui_source,
                ),
            )


def safe_record(
    analytics: RadarAnalytics,
    *,
    event_name: str,
    anonymous_session: str,
    repo: str,
    issue_number: int | None,
    ui_source: str,
    occurred_at: str | None = None,
) -> bool:
    """Record an event without ever failing the product path."""
    try:
        analytics.record(
            event_name=event_name,
            anonymous_session=anonymous_session,
            repo=repo,
            issue_number=issue_number,
            ui_source=ui_source,
            occurred_at=occurred_at,
        )
    except Exception as exc:  # Analytics must stay outside the product failure domain.
        LOGGER.warning("radar analytics degraded: %s", type(exc).__name__)
        return False
    return True
