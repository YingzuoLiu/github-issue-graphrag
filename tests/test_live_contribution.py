"""Contribution scoring: deterministic, explainable, and sensitive to the right events."""

from __future__ import annotations

import networkx as nx

from issue_graphrag.live.contribution import diff_opportunities, opportunities, score_issue


def graph_with(
    *,
    issue_state="open",
    labels=(),
    concepts=0,
    pr_state=None,
    pr_relation="closes",
    blocker_state=None,
    assignees=(),
) -> nx.Graph:
    graph = nx.Graph()
    graph.add_node(
        "Issue #1", type="ISSUE", state=issue_state, labels=list(labels),
        description="An issue", url="https://example.test/1", number=1,
    )

    for index in range(concepts):
        name = f"Concept {index}"
        graph.add_node(name, type="ALGORITHM", state=None, labels=[], url=None, number=None)
        graph.add_edge(
            "Issue #1", name,
            directed_relations=[{"source": "Issue #1", "target": name, "relation": "mentions", "origin": "llm"}],
        )

    if pr_state:
        graph.add_node("PR #2", type="PULL_REQUEST", state=pr_state, labels=[],
                       description="A pull request", url="https://example.test/2", number=2)
        graph.add_edge(
            "Issue #1", "PR #2",
            directed_relations=[
                {"source": "PR #2", "target": "Issue #1", "relation": pr_relation, "origin": "github"}
            ],
        )

    if blocker_state:
        graph.add_node("Issue #3", type="ISSUE", state=blocker_state, labels=[],
                       description="A blocker", url="https://example.test/3", number=3)
        graph.add_edge(
            "Issue #1", "Issue #3",
            directed_relations=[
                {"source": "Issue #1", "target": "Issue #3", "relation": "blocked_by", "origin": "github"}
            ],
        )

    for login in assignees:
        account = f"@{login}"
        graph.add_node(
            account,
            type="CONTRIBUTOR",
            state=None,
            labels=[],
            description=f"GitHub account {account}",
            url=f"https://github.com/{login}",
            number=None,
        )
        graph.add_edge(
            "Issue #1",
            account,
            directed_relations=[
                {
                    "source": "Issue #1",
                    "target": account,
                    "relation": "assigned_to",
                    "origin": "github",
                }
            ],
        )

    return graph


def test_a_plain_open_issue_is_available():
    item = score_issue(graph_with(), "Issue #1")

    assert (item.status, item.score) == ("available", 1.0)
    assert item.reasons == ["open issue (+1.00)"]


def test_help_wanted_and_linked_concepts_raise_the_score():
    item = score_issue(graph_with(labels=["help wanted"], concepts=3), "Issue #1")

    assert item.status == "available"
    assert item.score == 1.0 + 0.75 + 0.6
    assert any("help wanted" in reason for reason in item.reasons)
    assert any("3 linked technical concepts" in reason for reason in item.reasons)


def test_the_concept_bonus_is_capped():
    many = score_issue(graph_with(concepts=9), "Issue #1")
    capped = score_issue(graph_with(concepts=5), "Issue #1")

    assert many.score == capped.score


def test_an_open_pull_request_claims_the_issue():
    item = score_issue(graph_with(pr_state="open"), "Issue #1")

    assert item.status == "claimed"
    assert item.claimed_by == ["PR #2"]
    assert any("already picked up" in reason for reason in item.reasons)
    assert any(evidence.url == "https://example.test/2" for evidence in item.evidence)


def test_a_merged_pull_request_still_counts_as_claimed():
    assert score_issue(graph_with(pr_state="merged"), "Issue #1").status == "claimed"


def test_an_assignee_claims_the_issue_without_counting_as_a_concept():
    item = score_issue(graph_with(assignees=("octocat",)), "Issue #1")

    assert (item.status, item.score) == ("claimed", 0.0)
    assert item.assignees == ["@octocat"]
    assert item.claimed_by == []
    assert item.concepts == []
    assert any("assigned to @octocat" in reason for reason in item.reasons)
    assert any(
        evidence.label == "assigned to @octocat"
        and evidence.url == "https://example.test/1"
        for evidence in item.evidence
    )


def test_assignee_and_pull_request_apply_the_claimed_penalty_only_once():
    item = score_issue(
        graph_with(assignees=("octocat",), pr_state="open"),
        "Issue #1",
    )

    assert item.score == 0.0
    assert item.assignees == ["@octocat"]
    assert item.claimed_by == ["PR #2"]
    assert sum("(-2.00)" in reason for reason in item.reasons) == 1


def test_a_closed_unmerged_pull_request_releases_the_issue():
    item = score_issue(graph_with(pr_state="closed"), "Issue #1")

    assert item.status == "available"
    assert item.claimed_by == []


def test_a_mere_mention_from_a_pull_request_also_claims():
    item = score_issue(graph_with(pr_state="open", pr_relation="references"), "Issue #1")

    assert item.status == "claimed"


def test_an_open_blocker_penalises_and_a_closed_one_does_not():
    blocked = score_issue(graph_with(blocker_state="open"), "Issue #1")
    unblocked = score_issue(graph_with(blocker_state="closed"), "Issue #1")

    assert blocked.status == "blocked"
    assert blocked.blocked_by == ["Issue #3"]
    assert unblocked.status == "available"
    assert unblocked.score > blocked.score


def test_closed_issues_score_zero_and_drop_out_of_the_ranking():
    graph = graph_with(issue_state="closed")

    assert score_issue(graph, "Issue #1").status == "closed"
    assert opportunities(graph) == []
    assert [item.node for item in opportunities(graph, include_closed=True)] == ["Issue #1"]


def test_ranking_is_deterministic_and_breaks_ties_by_issue_number():
    graph = nx.Graph()
    for number in (30, 10, 20):
        graph.add_node(f"Issue #{number}", type="ISSUE", state="open", labels=[],
                       description="", url=None, number=number)

    assert [item.node for item in opportunities(graph)] == ["Issue #10", "Issue #20", "Issue #30"]


def test_diff_explains_an_issue_becoming_claimed():
    before = opportunities(graph_with())
    after = opportunities(graph_with(pr_state="open"))

    changes = diff_opportunities(before, after)

    assert len(changes) == 1
    assert changes[0].change == "status_changed"
    assert (changes[0].before_status, changes[0].after_status) == ("available", "claimed")
    assert any("already picked up" in reason for reason in changes[0].reasons)


def test_diff_reports_an_issue_leaving_the_ranking():
    changes = diff_opportunities(opportunities(graph_with()), opportunities(graph_with(issue_state="closed")))

    assert [change.change for change in changes] == ["disappeared"]
