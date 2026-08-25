from __future__ import annotations

import json

import pytest

from issue_graphrag.live.indexer import NullExtractor, apply_event, bootstrap
from issue_graphrag.live.radar import (
    RadarCoverage,
    RadarHistory,
    build_radar_snapshot,
    load_radar_snapshot,
    read_coverage,
    read_event_snapshot,
)
from issue_graphrag.live.records import seed_items
from issue_graphrag.live.repositories import RepoFreshness, RepoRegistry, write_freshness
from issue_graphrag.live.store import write_state


def _coverage() -> RadarCoverage:
    return RadarCoverage(status="bounded", message="bounded", item_limit=30)


def _freshness(repo: str, *, source: str = "current", semantic: str = "current"):
    return RepoFreshness(
        repo=repo,
        source_status=source,
        source_kind="scheduled_sync",
        last_source_sync_at="2026-08-24T02:00:00Z",
        semantic_status=semantic,
        semantic_updated_at=(
            "2026-08-24T02:00:00Z" if semantic == "current" else None
        ),
    )


def _replayed_snapshot(seeded_state, demo_events, extractor):  # noqa: ANN001
    events = demo_events[:2]
    for event in events:
        apply_event(seeded_state, event, extractor)
    return build_radar_snapshot(
        seeded_state,
        RadarHistory(status="current", events=tuple(events)),
        _freshness(seeded_state.repo),
        _coverage(),
    )


def _state(repo: str, number: int, title: str, *, state: str = "open", url=True):
    item_url = f"https://github.com/{repo}/issues/{number}" if url else None
    rows = [
        {
            "kind": "issue",
            "number": number,
            "title": title,
            "body": "",
            "state": state,
            "labels": [],
            "assignees": [],
            "author": "maintainer",
            "url": item_url,
            "created_at": "2026-08-24T01:00:00Z",
            "updated_at": "2026-08-24T01:00:00Z",
            "comments": {},
        }
    ]
    return bootstrap(repo, seed_items(repo, rows), NullExtractor())


def test_radar_maps_the_existing_deterministic_statuses_and_recent_changes(
    seeded_state,
    demo_events,
    extractor,
):
    snapshot = _replayed_snapshot(seeded_state, demo_events, extractor)
    statuses = {item.number: item.status for item in snapshot.opportunities}

    assert statuses == {875: "available", 901: "blocked", 922: "available", 944: "claimed"}
    assert snapshot.issue(875).status_label == "Ready"
    assert snapshot.count("available") == 2
    assert snapshot.count("claimed") == 1
    assert snapshot.count("blocked") == 1
    assert any(change.number == 944 for change in snapshot.recent_changes)
    assert all(
        reason.origin in {"github", "inference"} and reason.traceable
        for change in snapshot.recent_changes
        for reason in change.reasons
    )


def test_card_and_detail_keep_github_facts_and_inference_in_separate_contracts(
    seeded_state,
    demo_events,
    extractor,
):
    snapshot = _replayed_snapshot(seeded_state, demo_events, extractor)
    ready = snapshot.issue(875)
    claimed = snapshot.issue(944)

    assert ready is not None and claimed is not None
    assert any(reason.origin == "inference" for reason in ready.reasons)
    assert all(row.origin == "github" for row in ready.github_evidence)
    assert all(row.origin == "inference" for row in ready.inferred_evidence)
    assert ready.inferred_context
    assert all(inference.evidence for inference in ready.inferred_context)
    assert ready.evidence_complete

    assert claimed.claimed_by == ("PR #950",)
    assert claimed.pull_requests[0].number == 950
    assert claimed.pull_requests[0].files
    assert any("PR #950" in evidence.label for evidence in claimed.github_evidence)


def test_missing_source_links_are_explicitly_not_complete_evidence():
    state = _state("owner/repo", 7, "No source URL", url=False)
    snapshot = build_radar_snapshot(
        state,
        RadarHistory(status="not_started"),
        _freshness(state.repo),
        _coverage(),
    )

    issue = snapshot.issue(7)
    assert issue is not None and issue.status == "available"
    assert not issue.evidence_complete
    assert all(not reason.traceable for reason in issue.reasons)


def test_degraded_semantics_preserve_github_opportunities_without_claiming_current_inference(
    seeded_state,
    demo_events,
    extractor,
):
    events = demo_events[:2]
    for event in events:
        apply_event(seeded_state, event, extractor)
    snapshot = build_radar_snapshot(
        seeded_state,
        RadarHistory(status="current", events=tuple(events)),
        _freshness(seeded_state.repo, semantic="degraded"),
        _coverage(),
    )

    assert snapshot.freshness.semantic_status == "degraded"
    assert {item.status for item in snapshot.opportunities} == {
        "available",
        "claimed",
        "blocked",
    }
    assert snapshot.issue(875).github_facts


def test_read_only_event_snapshot_ignores_a_partial_tail_without_repairing_it(
    tmp_path,
    demo_events,
):
    path = tmp_path / "event_log.jsonl"
    complete = demo_events[0].model_dump_json().encode("utf-8") + b"\n"
    original = complete + b'{"delivery_id":"partial"'
    path.write_bytes(original)

    history = read_event_snapshot(path, demo_events[0].repo)

    assert history.status == "partial"
    assert [event.delivery_id for event in history.events] == [demo_events[0].delivery_id]
    assert path.read_bytes() == original


def test_event_snapshot_rejects_cross_repository_history(tmp_path, demo_events):
    path = tmp_path / "event_log.jsonl"
    event = demo_events[0].model_copy(update={"repo": "other/repository"})
    path.write_text(event.model_dump_json() + "\n", encoding="utf-8")

    history = read_event_snapshot(path, demo_events[0].repo)

    assert history.status == "unavailable"
    assert history.events == ()
    assert "current facts remain usable" in (history.message or "")


def test_reconciliation_changes_keep_their_observation_label(
    seeded_state,
    demo_events,
    extractor,
):
    event = demo_events[0].model_copy(update={"source": "reconciliation"})
    apply_event(seeded_state, event, extractor)

    snapshot = build_radar_snapshot(
        seeded_state,
        RadarHistory(status="current", events=(event,)),
        _freshness(seeded_state.repo),
        _coverage(),
    )

    assert snapshot.recent_changes
    assert all(
        change.source_label == "Observed during scheduled sync"
        for change in snapshot.recent_changes
    )


def test_coverage_is_bounded_unknown_or_unavailable_without_assuming_complete(tmp_path):
    missing = read_coverage(tmp_path / "missing.json")
    assert missing.status == "unknown"
    assert "cannot be confirmed" in missing.message

    bounded_path = tmp_path / "bounded.json"
    bounded_path.write_text(
        json.dumps(
            {
                "items": [{"number": 1}, {"number": 2}],
                "backfill": {
                    "item_limit": 2,
                    "comment_limit_per_item": 50,
                    "file_limit_per_pull": 100,
                },
            }
        ),
        encoding="utf-8",
    )
    bounded = read_coverage(bounded_path)
    assert bounded.status == "bounded" and bounded.cap_reached
    assert "older items may be absent" in bounded.message

    broken_path = tmp_path / "broken.json"
    broken_path.write_text("{", encoding="utf-8")
    broken = read_coverage(broken_path)
    assert broken.status == "unavailable"
    assert "results may be partial" in broken.message


def test_repository_loader_never_substitutes_or_combines_another_repo(tmp_path):
    registry = RepoRegistry(tmp_path)
    alpha = registry.register("alpha/one")
    beta = registry.register("beta/two")
    write_state(alpha.state, _state(alpha.repo, 1, "Alpha only"))
    write_state(beta.state, _state(beta.repo, 2, "Beta only"))
    write_freshness(alpha.freshness, _freshness(alpha.repo))
    write_freshness(beta.freshness, _freshness(beta.repo))

    first = load_radar_snapshot(alpha)
    second = load_radar_snapshot(beta)

    assert [(item.number, item.title) for item in first.opportunities] == [(1, "Alpha only")]
    assert [(item.number, item.title) for item in second.opportunities] == [(2, "Beta only")]

    write_state(alpha.state, _state(beta.repo, 9, "Wrong lane"))
    with pytest.raises(ValueError, match="state belongs"):
        load_radar_snapshot(alpha)


def test_no_open_issues_is_an_explicit_no_opportunity_result():
    state = _state("owner/repo", 7, "Already closed", state="closed")
    snapshot = build_radar_snapshot(
        state,
        RadarHistory(status="not_started"),
        _freshness(state.repo),
        _coverage(),
    )

    assert snapshot.opportunities == ()
    assert snapshot.issue(7).status == "closed"
