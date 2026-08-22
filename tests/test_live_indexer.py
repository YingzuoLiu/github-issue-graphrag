"""The five properties that decide whether the incremental index is trustworthy."""

from __future__ import annotations

import json

from conftest import make_event, issue_payload, pull_payload

from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.indexer import apply_event, rebuild, replay
from issue_graphrag.live.models import LiveState
from issue_graphrag.live.projection import graph_signature, project_graph
from issue_graphrag.live.store import read_state, write_state


def signature(state: LiveState, moment: str | None = None):
    return graph_signature(project_graph(state, moment))


def test_redelivering_the_same_event_changes_the_graph_only_once(
    seeded_state, demo_events, extractor
):
    """Acceptance 1: replay a webhook twice, the graph moves once."""
    first = demo_events[0]

    applied = apply_event(seeded_state, first, extractor)
    after_first = signature(seeded_state)
    assert applied.applied
    assert applied.fact_changes

    repeat = apply_event(seeded_state, first, extractor)

    assert not repeat.applied
    assert repeat.skip_reason == "duplicate delivery"
    assert repeat.fact_changes == []
    assert signature(seeded_state) == after_first


def test_fixture_replay_skips_the_redelivered_event(seeded_state, demo_events, extractor):
    deltas = replay(seeded_state, demo_events, extractor)
    skipped = [delta for delta in deltas if not delta.applied]

    assert len(demo_events) == 7
    assert [delta.delivery_id for delta in skipped] == ["d-0002"]
    assert len(seeded_state.processed_deliveries) == 6


def test_comment_deletion_invalidates_inferred_facts_but_keeps_history(
    seeded_state, demo_events, extractor
):
    """Acceptance 2: deleting a comment retires its inferences without erasing them."""
    replay(seeded_state, demo_events, extractor)

    elasticsearch = [
        fact
        for fact in seeded_state.facts
        if "Elasticsearch" in (fact.subject, fact.object)
    ]
    assert elasticsearch, "the fixture comment should have produced inferred facts"
    assert all(fact.valid_to == "2024-05-05T12:00:00Z" for fact in elasticsearch)
    assert all(fact.invalidated_by == "d-0004" for fact in elasticsearch)

    assert not project_graph(seeded_state).has_node("Elasticsearch")

    # The history stays queryable at any moment while the comment existed.
    while_present = project_graph(seeded_state, "2024-05-04T12:00:00Z")
    assert while_present.has_node("Elasticsearch")
    assert while_present.has_edge("Issue #875", "Elasticsearch")


def test_merged_pull_request_and_closed_issue_change_recommendations(
    seeded_state, demo_events, extractor
):
    """Acceptance 3: recommendations move when work is picked up and finished."""
    before = {item.node: item for item in opportunities(project_graph(seeded_state))}
    assert before["Issue #944"].status == "available"
    assert before["Issue #901"].status == "blocked"

    replay(seeded_state, demo_events, extractor)
    after = {item.node: item for item in opportunities(project_graph(seeded_state))}

    # #944 was claimed by PR #950 and then closed, so it leaves the ranking.
    assert "Issue #944" not in after
    # Closing #944 unblocks #901.
    assert after["Issue #901"].status == "available"
    assert after["Issue #901"].score > before["Issue #901"].score

    claimed = project_graph(seeded_state, "2024-05-03T10:00:00Z")
    mid = {item.node: item for item in opportunities(claimed)}
    assert mid["Issue #944"].status == "claimed"
    assert mid["Issue #944"].claimed_by == ["PR #950"]


def test_incremental_replay_matches_a_full_rebuild(
    seeded_state, demo_events, extractor, new_extractor
):
    """Acceptance 4: scoping the expensive work must not change the result."""
    replay(seeded_state, demo_events, extractor)
    fresh = rebuild(seeded_state, new_extractor())

    assert signature(seeded_state) == signature(fresh)
    assert [item.model_dump(exclude={"evidence"}) for item in opportunities(project_graph(seeded_state))] == [
        item.model_dump(exclude={"evidence"}) for item in opportunities(project_graph(fresh))
    ]


def test_every_inferred_fact_is_traceable_to_a_source(seeded_state, demo_events, extractor):
    """Acceptance 5: no inferred edge without an issue, comment or pull request behind it."""
    replay(seeded_state, demo_events, extractor)

    inferred = [fact for fact in seeded_state.facts if fact.origin == "llm"]
    assert inferred

    for fact in inferred:
        assert fact.evidence, f"{fact.label()} has no evidence"
        assert fact.document_id in seeded_state.items
        assert any(item.text_unit_id for item in fact.evidence), fact.label()
        assert any(item.url for item in fact.evidence), fact.label()


def test_state_only_events_do_not_trigger_re_extraction(seeded_state, demo_events, extractor):
    """Closing an issue changes facts and ranking without paying for extraction."""
    replay(seeded_state, demo_events[:5], extractor)
    calls_before = extractor.calls

    closing = demo_events[5]
    delta = apply_event(seeded_state, closing, extractor)

    assert closing.event_type == "issues"
    assert closing.action == "closed"
    assert delta.reextracted_documents == []
    assert extractor.calls == calls_before
    assert [fact.label() for fact in delta.changes_of("added")] == [
        "Issue #944 --has_state--> closed"
    ]


def test_reference_resolves_once_the_referenced_item_arrives(seeded_state, extractor):
    """A "#950" mention only becomes an edge when the index learns what 950 is."""
    mention = make_event(
        "d-ref-1",
        "issues",
        {
            "action": "edited",
            "issue": issue_payload(
                922,
                title="graph-rag latency",
                body="Superseded by the approach in #950.",
                updated_at="2024-05-10T09:00:00Z",
            ),
        },
        "2024-05-10T09:00:00Z",
    )
    apply_event(seeded_state, mention, extractor)
    assert not project_graph(seeded_state).has_node("PR #950")

    opened = make_event(
        "d-ref-2",
        "pull_request",
        {"action": "opened", "pull_request": pull_payload(950, body="Replaces the Pulsar hop.")},
        "2024-05-11T09:00:00Z",
    )
    apply_event(seeded_state, opened, extractor)

    graph = project_graph(seeded_state)
    assert graph.has_edge("Issue #922", "PR #950")
    assert "references" in graph.edges["Issue #922", "PR #950"]["relations"]


def test_state_survives_a_json_round_trip(seeded_state, demo_events, extractor, tmp_path):
    replay(seeded_state, demo_events, extractor)
    path = tmp_path / "live_state.json"
    write_state(path, seeded_state)

    restored = read_state(path)

    assert signature(restored) == signature(seeded_state)
    assert restored.processed_deliveries == seeded_state.processed_deliveries
    assert len(restored.facts) == len(seeded_state.facts)


def test_state_reader_migrates_record_versions_and_legacy_tombstones(
    seeded_state, tmp_path
):
    """A state written by the first v0.2 commit remains readable after the fix."""
    raw = seeded_state.model_dump()
    item = raw["items"]["trustgraph-ai/trustgraph#issue-875"]
    item.pop("field_versions")
    item["deleted_comments"] = {"7": "2024-05-05T12:00:00Z"}

    path = tmp_path / "legacy_state.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(raw, handle)

    restored = read_state(path)
    migrated = restored.items["trustgraph-ai/trustgraph#issue-875"]

    assert migrated.field_versions["title"].effective_at == migrated.effective_at
    assert migrated.field_versions["assignees"].effective_at == migrated.effective_at
    assert migrated.deleted_comments["7"].key() == ("2024-05-05T12:00:00Z", "")


def test_state_reader_backfills_only_a_new_field_version(seeded_state, tmp_path):
    raw = seeded_state.model_dump()
    item = raw["items"]["trustgraph-ai/trustgraph#issue-875"]
    title_version = item["field_versions"]["title"]
    item.pop("assignees")
    item["field_versions"].pop("assignees")

    path = tmp_path / "pre-assignee-state.json"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(raw, handle)

    migrated = read_state(path).items["trustgraph-ai/trustgraph#issue-875"]

    assert migrated.assignees == []
    assert migrated.field_versions["title"].model_dump() == title_version
    assert migrated.field_versions["assignees"].key() == migrated.version_key()
