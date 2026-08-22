"""The schema is the guard rail: what an LLM may assert, and what it may not."""

from __future__ import annotations

from conftest import REPO, make_event, pull_payload

from issue_graphrag.live.extraction import FixtureExtractor, llm_facts_for_item
from issue_graphrag.live.indexer import apply_event
from issue_graphrag.live.models import Evidence, Fact, LiveState, RepoItem
from issue_graphrag.live.ontology import (
    canonical_inferred_predicate,
    node_types_from,
    permits,
    validate_inferred,
)
from issue_graphrag.live.projection import project_graph


def inferred(subject: str, predicate: str, obj: str, kind: str = "relation") -> Fact:
    return Fact(
        kind=kind,
        subject=subject,
        predicate=predicate,
        object=obj,
        origin="llm",
        document_id="repo#issue-1",
        evidence=[Evidence(kind="body", ref="repo#issue-1")],
        observed_at="2024-05-01T00:00:00Z",
        valid_from="2024-05-01T00:00:00Z",
    )


def test_an_inferred_fact_may_not_assert_a_github_predicate():
    facts = [
        inferred("PR #950", "closes", "Issue #944"),
        inferred("Issue #944", "references", "Issue #901"),
        inferred("PR #950", "touches", "kafka_backend.py"),
        inferred("Issue #944", "has_state", "closed", kind="entity"),
    ]

    kept, rejected = validate_inferred(facts)

    assert kept == []
    assert len(rejected) == 4
    assert all("may only be asserted by GitHub" in reason or "not an inferable" in reason
               for _, reason in rejected)


def test_an_unknown_relation_label_folds_into_the_declared_vocabulary():
    kept, rejected = validate_inferred([inferred("BM25", "vaguely_similar_to", "RRF")])

    assert rejected == []
    assert kept[0].predicate == canonical_inferred_predicate("vaguely_similar_to") == "related_to"


def test_declared_inferred_relations_pass_through_unchanged():
    kept, rejected = validate_inferred([inferred("Bolt Traversal", "supersedes", "Pulsar")])

    assert rejected == []
    assert kept[0].predicate == "supersedes"


def test_domain_and_range_are_only_enforced_for_known_types():
    assert permits("closes", "PULL_REQUEST", "ISSUE")
    assert not permits("closes", "ISSUE", "ISSUE")
    assert not permits("touches", "ISSUE", "FILE")
    # An unknown endpoint type must not silently drop the edge.
    assert permits("closes", None, "ISSUE")


def test_github_typing_wins_over_an_inferred_type(seeded_state, extractor):
    """The LLM sees kafka_backend.py as a FILE too, but GitHub is the authority."""
    event = make_event(
        "d-file",
        "pull_request",
        {"action": "opened", "pull_request": pull_payload(950, body="Fixes #944, poll loop no longer holds it.")},
        "2024-05-03T10:00:00Z",
        attachments={"files": ["trustgraph-base/trustgraph/base/kafka_backend.py"]},
    )
    apply_event(seeded_state, event, extractor)

    node = project_graph(seeded_state).nodes["kafka_backend.py"]
    assert node["type"] == "FILE"
    assert "github" in node["origins"]


def test_node_types_prefer_the_deterministic_assignment():
    github = Fact(
        kind="entity", subject="kafka_backend.py", predicate="is_a", object="FILE",
        origin="github", document_id="repo#pull-1",
        observed_at="2024-05-01T00:00:00Z", valid_from="2024-05-01T00:00:00Z",
    )
    guess = inferred("kafka_backend.py", "is_a", "SCRIPT", kind="entity")

    assert node_types_from([guess, github])["kafka_backend.py"] == "FILE"


def test_extraction_output_without_evidence_is_not_stored():
    """An inference we cannot point at a source is not worth keeping."""
    item = RepoItem(kind="issue", repo=REPO, number=1, title="Empty", body="nothing relevant here")
    rules = [{"match": "nothing relevant", "entities": [{"name": "Ghost", "type": "CONCEPT"}]}]

    facts = llm_facts_for_item(item, [], FixtureExtractor(rules), "2024-05-01T00:00:00Z")

    assert facts == []


def test_a_violating_relation_is_dropped_at_projection_time():
    state = LiveState(repo=REPO)
    state.facts = [
        Fact(
            kind="entity", subject="Issue #1", predicate="is_a", object="ISSUE",
            origin="github", document_id="repo#issue-1",
            observed_at="2024-05-01T00:00:00Z", valid_from="2024-05-01T00:00:00Z",
        ),
        Fact(
            kind="entity", subject="a.py", predicate="is_a", object="FILE",
            origin="github", document_id="repo#pull-2",
            observed_at="2024-05-01T00:00:00Z", valid_from="2024-05-01T00:00:00Z",
        ),
        # Only a pull request may "touch" a file, so this inferred edge is dropped.
        inferred("Issue #1", "touches", "a.py"),
    ]

    graph = project_graph(state)

    assert graph.has_node("Issue #1") and graph.has_node("a.py")
    assert not graph.has_edge("Issue #1", "a.py")
