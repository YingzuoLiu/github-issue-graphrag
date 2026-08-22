"""Temporal correctness: no future knowledge, immutable facts, order convergence."""

from __future__ import annotations

import random

from conftest import REPO, issue_payload, make_event, pull_payload

from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.extraction import FixtureExtractor
from issue_graphrag.live.indexer import NullExtractor, apply_event, bootstrap, rebuild, replay
from issue_graphrag.live.models import Evidence, Fact, LiveState
from issue_graphrag.live.projection import alias_map, graph_signature, project_graph
from issue_graphrag.live.records import seed_items


def relation_fact(subject: str, predicate: str, obj: str, snippet: str = "s") -> Fact:
    return Fact(
        kind="relation",
        subject=subject,
        predicate=predicate,
        object=obj,
        origin="github",
        document_id=f"{REPO}#issue-1",
        evidence=[Evidence(kind="body", ref="r", snippet=snippet)],
        valid_from="2024-05-01T00:00:00Z",
    )


def typed(name: str, node_type: str) -> Fact:
    return Fact(
        kind="entity",
        subject=name,
        predicate="is_a",
        object=node_type,
        origin="github",
        document_id=f"{REPO}#issue-1",
        valid_from="2024-05-01T00:00:00Z",
    )


# --------------------------------------------------------------------------
# 1. A historical projection must not know what the index learned later.
# --------------------------------------------------------------------------


def test_a_past_moment_does_not_know_that_a_number_became_a_pull_request(seeded_state):
    """Before PR #950 exists, "#950" is just a mentioned number."""
    extractor = FixtureExtractor(
        [
            {
                "match": "approach in #950",
                "relationships": [
                    {"source": "#922", "target": "#950", "relation": "supersedes",
                     "description": "the comment points at 950"}
                ],
            }
        ]
    )

    mention = make_event(
        "d-1", "issue_comment",
        {
            "action": "created",
            "issue": issue_payload(922, body="latency", updated_at="2024-05-02T09:00:00Z"),
            "comment": {"id": 1, "body": "Superseded by the approach in #950.",
                        "user": {"login": "a"}, "created_at": "2024-05-02T09:00:00Z"},
        },
        "2024-05-02T09:00:00Z",
    )
    apply_event(seeded_state, mention, extractor)

    early = project_graph(seeded_state, "2024-05-02T09:00:00Z")
    assert early.has_node("Issue #950")
    assert not early.has_node("PR #950")

    opened = make_event(
        "d-2", "pull_request",
        {"action": "opened", "pull_request": pull_payload(950)},
        "2024-05-03T10:00:00Z",
    )
    apply_event(seeded_state, opened, extractor)

    now = project_graph(seeded_state)
    assert now.has_node("PR #950")
    assert not now.has_node("Issue #950")

    # The regression: replaying the same past moment after the pull request is
    # known must still show what was known then.
    replayed_past = project_graph(seeded_state, "2024-05-02T09:00:00Z")
    assert replayed_past.has_node("Issue #950")
    assert not replayed_past.has_node("PR #950")


def test_alias_resolution_reads_facts_not_records(seeded_state):
    """alias_map is a function of the fact set, so it is safe to time-travel."""
    facts = seeded_state.valid_facts()
    assert alias_map(facts) == alias_map(list(reversed(facts)))
    assert "Issue #944" not in alias_map(facts)


def test_a_past_moment_does_not_cite_text_written_later(seeded_state, extractor):
    """Grounding comes from the evidence snapshot, never from current chunks."""
    before = project_graph(seeded_state)
    original_units = set(before.nodes["Issue #901"]["source_ids"])

    edit = make_event(
        "d-1", "issue_comment",
        {
            "action": "created",
            "issue": issue_payload(
                901,
                title="Add integration tests for the Kafka backend",
                body="There is no end-to-end coverage. Blocked by #944 because the hang "
                     "makes the suite unrunnable.",
                updated_at="2024-05-09T09:00:00Z",
            ),
            "comment": {"id": 99, "body": "A" * 4000, "user": {"login": "a"},
                        "created_at": "2024-05-09T09:00:00Z"},
        },
        "2024-05-09T09:00:00Z",
    )
    apply_event(seeded_state, edit, extractor)

    past = project_graph(seeded_state, before.nodes["Issue #901"]["last_seen"])
    assert set(past.nodes["Issue #901"]["source_ids"]) == original_units


# --------------------------------------------------------------------------
# 2. Facts are immutable: a changed payload appends a version.
# --------------------------------------------------------------------------


def test_changed_evidence_appends_a_version_instead_of_editing_one(seeded_state, extractor):
    key_of = lambda fact: fact.key  # noqa: E731
    blocked = [
        fact for fact in seeded_state.facts
        if fact.predicate == "blocked_by" and fact.subject == "Issue #901"
    ]
    assert len(blocked) == 1
    original = blocked[0].model_copy(deep=True)

    edit = make_event(
        "d-1", "issues",
        {
            "action": "edited",
            "issue": issue_payload(
                901,
                title="Add integration tests for the Kafka backend",
                body="Cannot land this while the consumer teardown is broken: blocked by #944.",
                updated_at="2024-05-09T09:00:00Z",
            ),
        },
        "2024-05-09T09:00:00Z",
    )
    apply_event(seeded_state, edit, extractor)

    versions = [fact for fact in seeded_state.facts if key_of(fact) == key_of(original)]
    assert len(versions) == 2

    closed = [fact for fact in versions if fact.valid_to]
    live = [fact for fact in versions if not fact.valid_to]
    assert len(closed) == 1 and len(live) == 1

    # The retired version is byte-identical to what it was, apart from closing.
    assert closed[0].evidence == original.evidence
    assert closed[0].valid_from == original.valid_from
    assert closed[0].invalidated_by == "d-1"
    assert live[0].evidence != original.evidence
    assert live[0].valid_from == "2024-05-09T09:00:00Z"


def test_an_unchanged_fact_is_not_touched_by_an_unrelated_event(seeded_state, extractor):
    """A comment on #922 must not make every fact in the repo look re-observed."""
    untouched = {
        id(fact): fact.model_dump()
        for fact in seeded_state.facts
        if fact.document_id.endswith("issue-875")
    }

    event = make_event(
        "d-1", "issue_comment",
        {
            "action": "created",
            "issue": issue_payload(922, body="latency", updated_at="2024-05-02T09:00:00Z"),
            "comment": {"id": 1, "body": "unrelated note", "user": {"login": "a"},
                        "created_at": "2024-05-02T09:00:00Z"},
        },
        "2024-05-02T09:00:00Z",
    )
    delta = apply_event(seeded_state, event, extractor)

    for fact in seeded_state.facts:
        if id(fact) in untouched:
            assert fact.model_dump() == untouched[id(fact)]

    assert all(
        not change.fact.document_id.endswith("issue-875") for change in delta.fact_changes
    )


# --------------------------------------------------------------------------
# 3. Deliveries converge regardless of arrival order.
# --------------------------------------------------------------------------


def _final_signature(snapshot, events, rules_path):
    extractor = FixtureExtractor.from_path(rules_path)
    state = bootstrap(snapshot["repo"], seed_items(snapshot["repo"], snapshot["items"]), extractor)
    replay(state, events, extractor)
    return graph_signature(project_graph(state)), state


def test_out_of_order_deliveries_converge_on_the_same_graph(snapshot, demo_events):
    from conftest import FIXTURES

    rules = FIXTURES / "extraction_rules.json"
    ordered = [event for event in demo_events if event.delivery_id != "d-0002" or True]

    expected, _ = _final_signature(snapshot, ordered, rules)

    shuffler = random.Random(20240506)
    permutations = [list(reversed(ordered))]
    for _ in range(6):
        shuffled = list(ordered)
        shuffler.shuffle(shuffled)
        permutations.append(shuffled)

    for permutation in permutations:
        actual, state = _final_signature(snapshot, permutation, rules)
        order = [event.delivery_id for event in permutation]
        assert actual == expected, f"diverged for arrival order {order}"
        assert state.items[f"{REPO}#pull-950"].lifecycle_state() == "merged"
        assert state.items[f"{REPO}#issue-875"].comments == {}


def test_a_stale_payload_cannot_rewind_state(seeded_state, extractor):
    closed = make_event(
        "d-2", "issues",
        {"action": "closed", "issue": issue_payload(944, state="closed",
                                                    closed_at="2024-05-06T15:05:00Z",
                                                    updated_at="2024-05-06T15:05:00Z")},
        "2024-05-06T15:05:00Z",
    )
    apply_event(seeded_state, closed, extractor)

    late_but_older = make_event(
        "d-1", "issues",
        {"action": "edited", "issue": issue_payload(944, state="open",
                                                    updated_at="2024-05-04T00:00:00Z")},
        "2024-05-07T00:00:00Z",
    )
    apply_event(seeded_state, late_but_older, extractor)

    assert seeded_state.items[f"{REPO}#issue-944"].state == "closed"
    assert project_graph(seeded_state).nodes["Issue #944"]["state"] == "closed"


def test_assignee_add_and_remove_are_temporal_and_rebuild_safe(seeded_state, extractor):
    current = seeded_state.items[f"{REPO}#issue-944"]

    def payload(assignees, updated_at):  # noqa: ANN001, ANN202 - compact fixture helper
        return issue_payload(
            944,
            title=current.title,
            body=current.body,
            labels=[{"name": label} for label in current.labels],
            assignees=[{"login": login} for login in assignees],
            updated_at=updated_at,
        )

    assigned = apply_event(
        seeded_state,
        make_event(
            "d-assign",
            "issues",
            {
                "action": "assigned",
                "issue": payload(["octocat"], "2024-05-09T09:00:00Z"),
            },
            "2024-05-09T09:00:00Z",
        ),
        extractor,
    )
    at_assignment = opportunities(project_graph(seeded_state, assigned.indexed_at))
    assigned_item = next(item for item in at_assignment if item.number == 944)
    assert assigned_item.status == "claimed"
    assert assigned_item.assignees == ["@octocat"]

    removed = apply_event(
        seeded_state,
        make_event(
            "d-unassign",
            "issues",
            {
                "action": "unassigned",
                "issue": payload([], "2024-05-10T09:00:00Z"),
            },
            "2024-05-10T09:00:00Z",
        ),
        extractor,
    )

    current_item = next(
        item for item in opportunities(project_graph(seeded_state)) if item.number == 944
    )
    assert current_item.status == "available"
    assert current_item.assignees == []
    assert any(
        change.change == "invalidated" and change.fact.predicate == "assigned_to"
        for change in removed.fact_changes
    )

    still_historical = next(
        item
        for item in opportunities(project_graph(seeded_state, assigned.indexed_at))
        if item.number == 944
    )
    assert still_historical.status == "claimed"
    assert graph_signature(project_graph(rebuild(seeded_state))) == graph_signature(
        project_graph(seeded_state)
    )


# --------------------------------------------------------------------------
# 4. A partial payload must not demote a record.
# --------------------------------------------------------------------------


def test_a_comment_on_a_merged_pull_request_keeps_it_merged(seeded_state, extractor):
    """The issue-shaped comment payload carries no merged/draft fields."""
    files = ["trustgraph-base/trustgraph/base/kafka_backend.py"]

    apply_event(seeded_state, make_event(
        "d-1", "pull_request",
        {"action": "opened", "pull_request": pull_payload(950, body="Fixes #944.")},
        "2024-05-03T10:00:00Z", attachments={"files": files},
    ), extractor)

    apply_event(seeded_state, make_event(
        "d-2", "pull_request",
        {"action": "closed", "pull_request": pull_payload(
            950, body="Fixes #944.", state="closed", merged=True,
            merged_at="2024-05-06T15:00:00Z", updated_at="2024-05-06T15:00:00Z")},
        "2024-05-06T15:00:00Z", attachments={"files": files},
    ), extractor)

    document_id = f"{REPO}#pull-950"
    assert seeded_state.items[document_id].lifecycle_state() == "merged"

    apply_event(seeded_state, make_event(
        "d-3", "issue_comment",
        {
            "action": "created",
            "issue": {**issue_payload(950, state="closed", updated_at="2024-05-07T09:00:00Z"),
                      "pull_request": {"url": "https://api.github.com/pulls/950"}},
            "comment": {"id": 5, "body": "thanks", "user": {"login": "a"},
                        "created_at": "2024-05-07T09:00:00Z"},
        },
        "2024-05-07T09:00:00Z",
    ), extractor)

    item = seeded_state.items[document_id]
    assert item.kind == "pull_request"
    assert item.merged is True
    assert item.merged_at == "2024-05-06T15:00:00Z"
    assert item.lifecycle_state() == "merged"
    assert item.files == files
    assert project_graph(seeded_state).nodes["PR #950"]["state"] == "merged"


def test_a_deleted_comment_is_not_resurrected_by_a_late_create(seeded_state, extractor):
    comment = {"id": 7, "body": "Elasticsearch would do", "user": {"login": "a"},
               "created_at": "2024-05-04T08:30:00Z", "updated_at": "2024-05-04T08:30:00Z"}

    apply_event(seeded_state, make_event(
        "d-2", "issue_comment",
        {"action": "deleted",
         "issue": issue_payload(875, updated_at="2024-05-05T12:00:00Z"),
         "comment": comment},
        "2024-05-05T12:00:00Z",
    ), extractor)

    apply_event(seeded_state, make_event(
        "d-1", "issue_comment",
        {"action": "created",
         "issue": issue_payload(875, updated_at="2024-05-04T08:30:00Z"),
         "comment": comment},
        "2024-05-06T09:00:00Z",
    ), extractor)

    assert seeded_state.items[f"{REPO}#issue-875"].comments == {}


def test_a_stale_delete_cannot_remove_a_newer_comment_edit(seeded_state, extractor):
    """A delayed delete carries the old comment version and must not erase a newer edit."""
    original = {
        "id": 8,
        "body": "first draft",
        "user": {"login": "a"},
        "created_at": "2024-05-04T08:30:00Z",
        "updated_at": "2024-05-04T08:30:00Z",
    }
    edited = {
        **original,
        "body": "newer edit",
        "updated_at": "2024-05-06T08:30:00Z",
    }

    apply_event(seeded_state, make_event(
        "d-create", "issue_comment",
        {"action": "created", "issue": issue_payload(
            875, updated_at="2024-05-04T08:30:00Z"), "comment": original},
        "2024-05-04T08:30:00Z",
    ), extractor)
    apply_event(seeded_state, make_event(
        "d-edit", "issue_comment",
        {"action": "edited", "issue": issue_payload(
            875, updated_at="2024-05-06T08:30:00Z"), "comment": edited},
        "2024-05-06T08:30:00Z",
    ), extractor)

    # This delivery arrives last, but its source payload predates the edit.
    apply_event(seeded_state, make_event(
        "d-stale-delete", "issue_comment",
        {"action": "deleted", "issue": issue_payload(
            875, updated_at="2024-05-05T12:00:00Z"), "comment": original},
        "2024-05-07T09:00:00Z",
    ), extractor)

    item = seeded_state.items[f"{REPO}#issue-875"]
    assert item.comments["8"].body == "newer edit"
    assert "8" not in item.deleted_comments


def test_comment_delete_wins_same_timestamp_tie_in_either_order():
    """Deletion dominates an equally-timestamped edit, independent of delivery ids."""
    moment = "2024-05-06T08:30:00Z"
    comment = {
        "id": 10,
        "body": "remove this",
        "user": {"login": "a"},
        "created_at": "2024-05-04T08:30:00Z",
        "updated_at": moment,
    }
    parent = issue_payload(875, updated_at=moment)
    edited = make_event(
        "z-edit",
        "issue_comment",
        {"action": "edited", "issue": parent, "comment": comment},
        moment,
    )
    deleted = make_event(
        "a-delete",
        "issue_comment",
        {"action": "deleted", "issue": parent, "comment": comment},
        moment,
    )

    for events in ([edited, deleted], [deleted, edited]):
        state = LiveState(repo=REPO)
        replay(state, [event.model_copy(deep=True) for event in events], NullExtractor())

        item = state.items[f"{REPO}#issue-875"]
        assert item.comments == {}
        assert item.deleted_comments["10"].effective_at == moment


def test_same_timestamp_partial_pr_payloads_converge_in_either_order():
    """Field-wise versions keep a comment snapshot from hiding PR-only fields."""
    moment = "2024-05-06T15:00:00Z"
    pull = make_event(
        "a-pull", "pull_request",
        {"action": "closed", "pull_request": pull_payload(
            953, state="closed", merged=True, merged_at=moment, updated_at=moment)},
        moment,
        attachments={"files": ["src/merged.py"]},
    )
    comment = make_event(
        "z-comment", "issue_comment",
        {
            "action": "created",
            "issue": {
                **issue_payload(
                    953,
                    title="PR 953",
                    state="closed",
                    updated_at=moment,
                    html_url=f"https://github.com/{REPO}/pull/953",
                ),
                "pull_request": {"url": "https://api.github.com/pulls/953"},
            },
            "comment": {
                "id": 9,
                "body": "ship it",
                "user": {"login": "a"},
                "created_at": moment,
            },
        },
        moment,
    )

    states = []
    for events in ([pull, comment], [comment, pull]):
        state = LiveState(repo=REPO)
        replay(state, [event.model_copy(deep=True) for event in events], NullExtractor())
        states.append(state)

    assert graph_signature(project_graph(states[0])) == graph_signature(project_graph(states[1]))
    for state in states:
        item = state.items[f"{REPO}#pull-953"]
        assert item.lifecycle_state() == "merged"
        assert item.files == ["src/merged.py"]
        assert item.comments["9"].body == "ship it"


def test_events_received_in_the_same_second_get_distinct_history_windows(
    seeded_state, extractor
):
    """A timeline must retain the intermediate graph even at second precision."""
    moment = "2024-05-09T09:00:00Z"
    closed = make_event(
        "d-close", "issues",
        {"action": "closed", "issue": issue_payload(
            944, state="closed", closed_at=moment, updated_at=moment)},
        moment,
    )
    reopened = make_event(
        "d-reopen", "issues",
        {"action": "reopened", "issue": issue_payload(
            944, state="open", closed_at=None, updated_at=moment)},
        moment,
    )

    first = apply_event(seeded_state, closed, extractor)
    second = apply_event(seeded_state, reopened, extractor)

    assert first.indexed_at < second.indexed_at
    assert project_graph(seeded_state, first.indexed_at).nodes["Issue #944"]["state"] == "closed"
    assert project_graph(seeded_state, second.indexed_at).nodes["Issue #944"]["state"] == "open"


# --------------------------------------------------------------------------
# 5. The rebuild signature has to be able to fail.
# --------------------------------------------------------------------------


def test_signature_distinguishes_edge_direction():
    """Both directions are legal here, so only the signature can tell them apart.

    A reversed ``closes`` would be caught by the ontology instead, which would
    make this test pass for the wrong reason. ``references`` accepts issue to
    issue either way, so the direction has to survive into the fingerprint.
    """
    forward = LiveState(repo=REPO, facts=[
        typed("Issue #1", "ISSUE"), typed("Issue #2", "ISSUE"),
        relation_fact("Issue #1", "references", "Issue #2"),
    ])
    backward = LiveState(repo=REPO, facts=[
        typed("Issue #1", "ISSUE"), typed("Issue #2", "ISSUE"),
        relation_fact("Issue #2", "references", "Issue #1"),
    ])

    # Same undirected edge, same relation label: only direction differs.
    assert project_graph(forward).has_edge("Issue #1", "Issue #2")
    assert project_graph(backward).has_edge("Issue #1", "Issue #2")
    assert graph_signature(project_graph(forward)) != graph_signature(project_graph(backward))


def test_signature_distinguishes_evidence():
    left = LiveState(repo=REPO, facts=[
        typed("Issue #1", "ISSUE"), typed("Issue #2", "ISSUE"),
        relation_fact("Issue #1", "references", "Issue #2", snippet="see #2"),
    ])
    right = LiveState(repo=REPO, facts=[
        typed("Issue #1", "ISSUE"), typed("Issue #2", "ISSUE"),
        relation_fact("Issue #1", "references", "Issue #2", snippet="fabricated"),
    ])

    assert graph_signature(project_graph(left)) != graph_signature(project_graph(right))


def test_rebuild_without_an_extractor_reuses_recorded_extraction(
    seeded_state, demo_events, extractor
):
    """The default rebuild check is about this pipeline, not the model."""
    replay(seeded_state, demo_events, extractor)
    calls_before = extractor.calls

    fresh = rebuild(seeded_state)

    assert extractor.calls == calls_before
    assert graph_signature(project_graph(fresh)) == graph_signature(project_graph(seeded_state))
    assert any(fact.origin == "llm" for fact in fresh.facts)


# --------------------------------------------------------------------------
# 6. File identity is the path, not the basename.
# --------------------------------------------------------------------------


def test_same_named_files_in_different_directories_stay_separate(seeded_state, extractor):
    event = make_event(
        "d-1", "pull_request",
        {"action": "opened", "pull_request": pull_payload(951, body="Config cleanup.")},
        "2024-05-03T10:00:00Z",
        attachments={"files": ["src/config.py", "tests/config.py"]},
    )
    apply_event(seeded_state, event, extractor)
    graph = project_graph(seeded_state)

    assert graph.has_node("src/config.py")
    assert graph.has_node("tests/config.py")
    assert not graph.has_node("config.py")
    assert graph.has_edge("PR #951", "src/config.py")
    assert graph.has_edge("PR #951", "tests/config.py")
    assert not graph.has_edge("src/config.py", "tests/config.py")

    # An ambiguous basename is left as its own node rather than merged into one
    # of them, which would hand it the edges of a file it is not.
    assert "config.py" not in alias_map(seeded_state.valid_facts())


def test_an_unambiguous_basename_resolves_onto_its_path(seeded_state, extractor):
    event = make_event(
        "d-1", "pull_request",
        {"action": "opened", "pull_request": pull_payload(952, body="Kafka fix.")},
        "2024-05-03T10:00:00Z",
        attachments={"files": ["trustgraph-base/trustgraph/base/kafka_backend.py"]},
    )
    apply_event(seeded_state, event, extractor)

    aliases = alias_map(seeded_state.valid_facts())
    assert aliases["kafka_backend.py"] == "trustgraph-base/trustgraph/base/kafka_backend.py"


# --------------------------------------------------------------------------
# Ontology guards the deterministic path too.
# --------------------------------------------------------------------------


def test_a_closing_keyword_in_an_issue_is_a_reference_not_a_close(seeded_state, extractor):
    """Only a pull request may close an issue, so an issue saying "fixes #944" references it."""
    event = make_event(
        "d-1", "issues",
        {"action": "edited", "issue": issue_payload(
            901, title="Add integration tests", body="Fixes #944 once the hang is gone.",
            updated_at="2024-05-09T09:00:00Z")},
        "2024-05-09T09:00:00Z",
    )
    apply_event(seeded_state, event, extractor)

    graph = project_graph(seeded_state)
    relations = graph.edges["Issue #901", "Issue #944"]["relations"]
    assert "references" in relations
    assert "closes" not in relations
