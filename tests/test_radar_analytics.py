from __future__ import annotations

import sqlite3

import pytest

from issue_graphrag.live.analytics import (
    RADAR_ANALYTICS_EVENTS,
    RADAR_ANALYTICS_SOURCES,
    RadarAnalytics,
    safe_record,
)


def test_all_five_events_use_the_closed_anonymous_minimal_schema(tmp_path):
    path = tmp_path / "radar.sqlite"
    analytics = RadarAnalytics(path)

    for index, event_name in enumerate(RADAR_ANALYTICS_EVENTS, start=1):
        ui_source = sorted(RADAR_ANALYTICS_SOURCES[event_name])[0]
        analytics.record(
            event_name=event_name,
            anonymous_session="anonymous-random-token",
            repo="Owner/Repo",
            issue_number=index if index >= 3 else None,
            occurred_at=f"2026-08-24T02:00:0{index}Z",
            ui_source=ui_source,
        )

    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(radar_events)")]
        rows = connection.execute(
            """
            SELECT event_name, anonymous_session, repo, issue_number, occurred_at, ui_source
            FROM radar_events ORDER BY id
            """
        ).fetchall()

    assert columns == [
        "id",
        "event_name",
        "anonymous_session",
        "repo",
        "issue_number",
        "occurred_at",
        "ui_source",
    ]
    assert [row[0] for row in rows] == list(RADAR_ANALYTICS_EVENTS)
    assert all(row[1] == "anonymous-random-token" for row in rows)
    assert all(row[2] == "owner/repo" for row in rows)
    assert not {"title", "body", "comment", "question", "github_identity", "url"} & set(columns)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"event_name": "unknown"}, "unsupported"),
        ({"anonymous_session": "github-user"}, "random URL-safe token"),
        ({"issue_number": 0}, "must be positive"),
        ({"ui_source": "issue body copied here"}, "unsupported UI source"),
    ],
)
def test_invalid_or_expansive_event_shapes_fail_closed(tmp_path, updates, message):
    arguments = {
        "event_name": "opportunity_opened",
        "anonymous_session": "anonymous-random-token",
        "repo": "owner/repo",
        "issue_number": 7,
        "occurred_at": "2026-08-24T02:00:00Z",
        "ui_source": "radar_card",
    }
    arguments.update(updates)

    with pytest.raises(ValueError, match=message):
        RadarAnalytics(tmp_path / "radar.sqlite").record(**arguments)


def test_analytics_failure_never_interrupts_the_product_path(tmp_path):
    impossible_parent = tmp_path / "not-a-directory"
    impossible_parent.write_text("occupied", encoding="utf-8")

    recorded = safe_record(
        RadarAnalytics(impossible_parent / "radar.sqlite"),
        event_name="radar_viewed",
        anonymous_session="anonymous-random-token",
        repo="owner/repo",
        issue_number=None,
        occurred_at="2026-08-24T02:00:00Z",
        ui_source="radar_page",
    )

    assert recorded is False


def test_github_opened_accepts_only_real_outbound_surfaces(tmp_path):
    analytics = RadarAnalytics(tmp_path / "radar.sqlite")

    for source in ("radar_card", "issue_detail"):
        analytics.record(
            event_name="github_opened",
            anonymous_session="anonymous-random-token",
            repo="owner/repo",
            issue_number=7,
            occurred_at="2026-08-24T02:00:00Z",
            ui_source=source,
        )

    with sqlite3.connect(tmp_path / "radar.sqlite") as connection:
        sources = [
            row[0]
            for row in connection.execute(
                "SELECT ui_source FROM radar_events WHERE event_name = 'github_opened' ORDER BY id"
            ).fetchall()
        ]
    assert sources == ["radar_card", "issue_detail"]
