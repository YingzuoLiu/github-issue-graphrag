from __future__ import annotations

import pytest

from conftest import REPO, issue_payload, make_event

from issue_graphrag.ingest.github_loader import to_seed_item
from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.indexer import NullExtractor, apply_event, bootstrap, rebuild
from issue_graphrag.live.models import RepoItem
from issue_graphrag.live.projection import graph_signature, project_graph

SEED_AT = "2024-05-01T00:00:00Z"


def _state(*, locked: bool = False, blocking_dependency_count: int = 0):
    item = RepoItem(
        kind="issue",
        repo=REPO,
        number=1,
        title="A constrained issue",
        body="",
        state="open",
        locked=locked,
        blocking_dependency_count=blocking_dependency_count,
        url=f"https://github.com/{REPO}/issues/1",
        created_at=SEED_AT,
        updated_at=SEED_AT,
        effective_at=SEED_AT,
    )
    item.seed_field_versions()
    return bootstrap(REPO, {item.document_id: item}, NullExtractor())


def _opportunity(state):  # noqa: ANN001, ANN202 - compact assertion helper
    return opportunities(project_graph(state))[0]


def test_seed_normalization_uses_active_dependency_count_with_legacy_fallback():
    base = {
        "number": 1,
        "title": "Issue",
        "body": "",
        "state": "open",
        "labels": [],
        "assignees": [],
        "user": {"login": "author"},
    }

    clear = to_seed_item(
        REPO,
        {
            **base,
            "locked": False,
            "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 2},
        },
        "issue",
    )
    constrained = to_seed_item(
        REPO,
        {
            **base,
            "locked": True,
            "issue_dependencies_summary": {"blocked_by": 1, "total_blocked_by": 2},
        },
        "issue",
    )
    legacy = to_seed_item(
        REPO,
        {
            **base,
            "issue_dependencies_summary": {"total_blocked_by": 2},
        },
        "issue",
    )

    assert "locked" not in clear
    assert "blocking_dependency_count" not in clear
    assert constrained["locked"] is True
    assert constrained["blocking_dependency_count"] == 1
    assert legacy["blocking_dependency_count"] == 2


@pytest.mark.parametrize(
    ("field", "value", "predicate"),
    [
        ("locked", True, "is_locked"),
        ("blocking_dependency_count", 2, "has_blocking_dependencies"),
    ],
)
def test_each_platform_fact_is_causal_traceable_and_mutation_sensitive(
    field,
    value,
    predicate,
):  # noqa: ANN001
    state = _state(**{field: value})
    facts = [fact for fact in state.valid_facts() if fact.predicate == predicate]

    assert len(facts) == 1
    assert facts[0].origin == "github"
    assert facts[0].evidence[0].url == f"https://github.com/{REPO}/issues/1"
    assert _opportunity(state).status == "blocked"

    mutated = state.model_copy(deep=True)
    mutated.facts = [fact for fact in mutated.facts if fact.predicate != predicate]

    assert _opportunity(mutated).status == "available"
    assert graph_signature(project_graph(mutated)) != graph_signature(project_graph(state))


def test_partial_and_stale_payloads_cannot_clear_newer_platform_constraints():
    state = _state()
    constrained = apply_event(
        state,
        make_event(
            "constraint-new",
            "issues",
            {
                "action": "edited",
                "issue": issue_payload(
                    1,
                    title="A constrained issue",
                    locked=True,
                    issue_dependencies_summary={"blocked_by": 1},
                    updated_at="2024-05-03T00:00:00Z",
                ),
            },
            "2024-05-03T00:00:00Z",
        ),
        NullExtractor(),
    )
    assert _opportunity(state).status == "blocked"

    # Comment parents are partial issue payloads. Their missing fields must not
    # be interpreted as an unlock or dependency removal.
    apply_event(
        state,
        make_event(
            "partial-comment",
            "issue_comment",
            {
                "action": "created",
                "issue": issue_payload(
                    1,
                    title="A constrained issue",
                    updated_at="2024-05-04T00:00:00Z",
                ),
                "comment": {
                    "id": 10,
                    "body": "still investigating",
                    "user": {"login": "reader"},
                    "created_at": "2024-05-04T00:00:00Z",
                },
            },
            "2024-05-04T00:00:00Z",
        ),
        NullExtractor(),
    )
    assert state.items[f"{REPO}#issue-1"].locked is True
    assert state.items[f"{REPO}#issue-1"].blocking_dependency_count == 1

    # A delayed older full payload explicitly says clear, but its source clock
    # loses independently for both fields.
    apply_event(
        state,
        make_event(
            "constraint-stale",
            "issues",
            {
                "action": "edited",
                "issue": issue_payload(
                    1,
                    title="A constrained issue",
                    locked=False,
                    issue_dependencies_summary={"blocked_by": 0},
                    updated_at="2024-05-02T00:00:00Z",
                ),
            },
            "2024-05-05T00:00:00Z",
        ),
        NullExtractor(),
    )
    assert _opportunity(state).status == "blocked"

    cleared = apply_event(
        state,
        make_event(
            "constraint-clear",
            "issues",
            {
                "action": "edited",
                "issue": issue_payload(
                    1,
                    title="A constrained issue",
                    locked=False,
                    issue_dependencies_summary={"blocked_by": 0},
                    updated_at="2024-05-06T00:00:00Z",
                ),
            },
            "2024-05-06T00:00:00Z",
        ),
        NullExtractor(),
    )

    assert _opportunity(state).status == "available"
    assert {
        change.fact.predicate
        for change in cleared.fact_changes
        if change.change == "invalidated"
    } >= {"is_locked", "has_blocking_dependencies"}
    historical = opportunities(project_graph(state, constrained.indexed_at))[0]
    assert historical.status == "blocked"
    assert graph_signature(project_graph(rebuild(state))) == graph_signature(project_graph(state))
