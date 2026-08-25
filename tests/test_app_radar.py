from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from issue_graphrag.live.analytics import RADAR_ANALYTICS_EVENTS
from issue_graphrag.live.indexer import NullExtractor, apply_event, bootstrap
from issue_graphrag.live.records import seed_items
from issue_graphrag.live.repositories import RepoFreshness, RepoRegistry, write_freshness
from issue_graphrag.live.store import write_state

APP = Path(__file__).resolve().parents[1] / "app.py"


def _configure(monkeypatch, root: Path, repos: tuple[str, ...]) -> None:  # noqa: ANN001
    monkeypatch.setenv("REPO_DATA_DIR", str(root))
    monkeypatch.setenv("GITHUB_REPOS", ",".join(repos))
    monkeypatch.delenv("GITHUB_WEBHOOK_REPO", raising=False)


def _freshness(
    repo: str,
    *,
    source: str = "current",
    semantic: str = "current",
    source_error: str | None = None,
) -> RepoFreshness:
    return RepoFreshness(
        repo=repo,
        source_status=source,
        source_kind="scheduled_sync",
        source_error=source_error,
        last_source_sync_at="2026-08-24T02:00:00Z",
        next_source_sync_at="2026-08-24T02:15:00Z",
        semantic_status=semantic,
        semantic_updated_at=(
            "2026-08-24T02:00:00Z" if semantic == "current" else None
        ),
    )


def _single_issue_state(repo: str, number: int, title: str, *, closed: bool = False):
    return bootstrap(
        repo,
        seed_items(
            repo,
            [
                {
                    "kind": "issue",
                    "number": number,
                    "title": title,
                    "body": "",
                    "state": "closed" if closed else "open",
                    "labels": ["help wanted"],
                    "assignees": [],
                    "author": "maintainer",
                    "url": f"https://github.com/{repo}/issues/{number}",
                    "created_at": "2026-08-24T01:00:00Z",
                    "updated_at": "2026-08-24T01:00:00Z",
                    "comments": {},
                }
            ],
        ),
        NullExtractor(),
    )


def _write_repo(
    root: Path,
    repo: str,
    state,
    *,
    freshness: RepoFreshness | None = None,
    events=(),  # noqa: ANN001
    coverage: bool = True,
):
    paths = RepoRegistry(root).register(repo)
    write_state(paths.state, state)
    write_freshness(paths.freshness, freshness or _freshness(repo))
    if events:
        paths.event_log.write_text(
            "".join(event.model_dump_json() + "\n" for event in events),
            encoding="utf-8",
        )
    if coverage:
        paths.bootstrap_seed.write_text(
            json.dumps(
                {
                    "repo": repo,
                    "items": [{"number": item.number} for item in state.items.values()],
                    "backfill": {
                        "item_limit": 30,
                        "comment_limit_per_item": 50,
                        "file_limit_per_pull": 100,
                    },
                }
            ),
            encoding="utf-8",
        )
    return paths


def _run() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=10).run()


def _text(app: AppTest) -> str:
    groups = (
        app.markdown,
        app.caption,
        app.info,
        app.warning,
        app.error,
        app.success,
        app.title,
        app.subheader,
    )
    return "\n".join(str(element.value) for group in groups for element in group)


def test_radar_is_default_and_opens_traceable_issue_detail(
    tmp_path,
    monkeypatch,
    seeded_state,
    demo_events,
    extractor,
):
    events = demo_events[:2]
    for event in events:
        apply_event(seeded_state, event, extractor)
    paths = _write_repo(tmp_path, seeded_state.repo, seeded_state, events=events)
    state_before = paths.state.read_bytes()
    event_log_before = paths.event_log.read_bytes()
    _configure(monkeypatch, tmp_path, (seeded_state.repo,))

    app = _run()

    assert not app.exception
    assert app.sidebar.radio[0].value == "Contribution Radar"
    assert app.radio[0].options == ["Ready", "Claimed", "Blocked", "Recently changed"]
    assert app.radio[0].value == "Ready"
    shell_css = app.markdown[0].value
    assert "@media (max-width: 700px)" in shell_css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in shell_css
    assert "Find the issue you can act on now" in _text(app)
    assert "GITHUB FACT" in _text(app)
    assert "INFERRED CONTEXT" in _text(app)
    assert [button.label for button in app.button] == [
        "View #875 details",
        "View #922 details",
    ]

    app.radio[0].set_value("Claimed").run()
    assert [button.label for button in app.button] == ["View #944 details"]
    app.radio[0].set_value("Blocked").run()
    assert [button.label for button in app.button] == ["View #901 details"]
    app.radio[0].set_value("Recently changed").run()
    assert "Latest recorded status or score change per issue" in _text(app)
    assert any(button.label.startswith("Inspect change to #") for button in app.button)
    app.radio[0].set_value("Ready").run()

    app.button[0].click().run()
    detail = _text(app)
    assert "## #875 · Improve document retrieval with hybrid retrieval" in detail
    assert "### GitHub-stated facts" in detail
    assert "### Inferred context" in detail
    evidence_button = next(
        button for button in app.button if button.label == "Show source evidence"
    )
    evidence_button.click().run()
    evidence = _text(app)
    assert "#### GitHub evidence" in evidence
    assert "https://github.com/trustgraph-ai/trustgraph/issues/875" in evidence
    github_button = next(button for button in app.button if button.label == "Open on GitHub")
    github_button.click().run()
    assert "You are leaving the read-only Radar" in _text(app)

    with sqlite3.connect(tmp_path / "radar_analytics.sqlite") as connection:
        event_names = [
            row[0]
            for row in connection.execute(
                "SELECT event_name FROM radar_events ORDER BY id"
            ).fetchall()
        ]
    assert event_names == list(RADAR_ANALYTICS_EVENTS)

    app.sidebar.radio[0].set_value("Timeline & graph").run()
    assert app.title[0].value == "Timeline & graph"
    assert "Secondary inspection tools" in _text(app)
    assert paths.state.read_bytes() == state_before
    assert paths.event_log.read_bytes() == event_log_before


def test_repo_switch_discards_previous_cards_detail_and_counts(tmp_path, monkeypatch):
    alpha = "alpha/one"
    beta = "beta/two"
    _write_repo(tmp_path, alpha, _single_issue_state(alpha, 11, "Alpha only"))
    _write_repo(tmp_path, beta, _single_issue_state(beta, 22, "Beta only"))
    _configure(monkeypatch, tmp_path, (alpha, beta))

    app = _run()
    assert "Alpha only" in _text(app)
    assert "Beta only" not in _text(app)
    app.button[0].click().run()
    assert "## #11 · Alpha only" in _text(app)

    app.selectbox[0].set_value(beta).run()
    switched = _text(app)
    assert "Beta only" in switched
    assert "Alpha only" not in switched
    assert "## #11" not in switched
    assert [button.label for button in app.button] == ["View #22 details"]


def test_empty_configuration_and_initial_loading_are_actionable(tmp_path, monkeypatch):
    _configure(monkeypatch, tmp_path / "empty", ())
    empty = _run()
    assert not empty.exception
    assert "No repositories are configured yet" in _text(empty)

    root = tmp_path / "loading"
    repo = "owner/loading"
    RepoRegistry(root).register(repo)
    _configure(monkeypatch, root, (repo,))
    loading = _run()
    assert not loading.exception
    assert "Loading initial repository index" in _text(loading)
    assert "fetch_live_seed.py" in loading.code[0].value


def test_repository_error_fails_closed_with_retry(tmp_path, monkeypatch):
    repo = "owner/broken"
    paths = RepoRegistry(tmp_path).register(repo)
    paths.state.write_text("{}\n", encoding="utf-8")
    _configure(monkeypatch, tmp_path, (repo,))

    app = _run()

    assert not app.exception
    assert "could not be loaded safely" in _text(app)
    assert "No data from another repository has been substituted" in _text(app)
    assert [button.label for button in app.button] == ["Retry repository load"]


def test_stale_last_good_data_is_never_described_as_current(tmp_path, monkeypatch):
    repo = "owner/stale"
    state = _single_issue_state(repo, 31, "Last-good issue")
    _write_repo(
        tmp_path,
        repo,
        state,
        freshness=_freshness(
            repo,
            source="stale",
            source_error="rate limit prevented refresh",
        ),
    )
    _configure(monkeypatch, tmp_path, (repo,))

    app = _run()

    assert not app.exception
    text = _text(app)
    assert "Showing the last-good GitHub facts" in text
    assert "rate limit prevented refresh" in text
    assert "GitHub facts current" not in text
    assert "Last-good issue" in text


def test_missing_freshness_timestamps_fail_visibly_degraded(tmp_path, monkeypatch):
    repo = "owner/unconfirmed"
    state = _single_issue_state(repo, 32, "Unconfirmed freshness")
    _write_repo(
        tmp_path,
        repo,
        state,
        freshness=RepoFreshness(
            repo=repo,
            source_status="current",
            semantic_status="current",
        ),
    )
    _configure(monkeypatch, tmp_path, (repo,))

    app = _run()

    assert not app.exception
    text = _text(app)
    assert "GitHub freshness cannot be confirmed" in text
    assert "Semantic freshness cannot be confirmed" in text
    assert "GitHub facts current" not in text
    assert "Unconfirmed freshness" in text


@pytest.mark.parametrize(
    ("semantic", "expected"),
    [
        pytest.param(
            "pending",
            "semantic enrichment is pending",
            id="pending",
        ),
        pytest.param(
            "degraded",
            "Inferred context is delayed",
            id="degraded",
        ),
    ],
)
def test_semantic_failure_keeps_github_facts_visible(
    tmp_path,
    monkeypatch,
    semantic,
    expected,
):
    repo = f"owner/{semantic}"
    title = f"GitHub facts survive {semantic} semantics"
    state = _single_issue_state(repo, 41, title)
    _write_repo(
        tmp_path,
        repo,
        state,
        freshness=_freshness(repo, semantic=semantic),
    )
    _configure(monkeypatch, tmp_path, (repo,))

    app = _run()

    assert not app.exception
    assert expected in _text(app)
    assert title in _text(app)
    assert "GITHUB FACT" in _text(app)


def test_no_opportunities_and_partial_metadata_are_explicit(tmp_path, monkeypatch):
    closed_repo = "owner/closed"
    _write_repo(
        tmp_path / "closed",
        closed_repo,
        _single_issue_state(closed_repo, 51, "Already closed", closed=True),
    )
    _configure(monkeypatch, tmp_path / "closed", (closed_repo,))
    no_opportunities = _run()
    assert "No issue is currently confirmed Ready" in _text(no_opportunities)
    assert not [button for button in no_opportunities.button if "details" in button.label]

    partial_repo = "owner/partial"
    paths = _write_repo(
        tmp_path / "partial",
        partial_repo,
        _single_issue_state(partial_repo, 52, "Current fact survives partial history"),
        coverage=False,
    )
    paths.event_log.write_bytes(b'{"delivery_id":"partial"')
    _configure(monkeypatch, tmp_path / "partial", (partial_repo,))
    partial = _run()
    text = _text(partial)
    assert "Coverage bounds are not recorded" in text
    assert "A partial final event was ignored" in text
    assert "Current fact survives partial history" in text


def test_analytics_failure_does_not_block_the_radar(tmp_path, monkeypatch):
    repo = "owner/analytics"
    _write_repo(tmp_path, repo, _single_issue_state(repo, 61, "Still actionable"))
    (tmp_path / "radar_analytics.sqlite").mkdir()
    _configure(monkeypatch, tmp_path, (repo,))

    app = _run()

    assert not app.exception
    assert "Still actionable" in _text(app)
    assert [button.label for button in app.button] == ["View #61 details"]
