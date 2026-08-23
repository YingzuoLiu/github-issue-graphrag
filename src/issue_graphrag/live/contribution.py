"""Deterministic contribution scoring on top of the projected graph.

Nothing here calls a model. The ranking is a small, auditable formula over facts
GitHub stated plus concepts the ontology accepted, which is what lets the demo
say *why* a recommendation changed after an event rather than just that it did.
"""

from __future__ import annotations

import networkx as nx

from issue_graphrag.live.models import Opportunity, OpportunityChange, OpportunityEvidence
from issue_graphrag.live.ontology import is_concept

BASE_SCORE = 1.0
HELP_WANTED_BONUS = 0.75
CONCEPT_BONUS = 0.2
CONCEPT_CAP = 5
CLAIMED_PENALTY = 2.0
BLOCKED_PENALTY = 1.5

HELP_LABELS = {
    "good first issue",
    "good-first-issue",
    "help wanted",
    "help-wanted",
    "documentation",
}

#: Pull request states that mean somebody is already on it.
ACTIVE_PR_STATES = ("open", "draft", "merged")


def _directed(graph: nx.Graph, node: str, other: str) -> list[dict]:
    if not graph.has_edge(node, other):
        return []
    return list(graph.edges[node, other].get("directed_relations", []))


def _claim_links(graph: nx.Graph, issue: str) -> list[tuple[str, str]]:
    """Pull requests that close or reference this issue, with the relation used."""
    links: list[tuple[str, str]] = []
    for neighbour in graph.neighbors(issue):
        data = graph.nodes[neighbour]
        if data.get("type") != "PULL_REQUEST":
            continue
        if data.get("state") not in ACTIVE_PR_STATES:
            continue
        for row in _directed(graph, issue, neighbour):
            if row.get("origin") != "github":
                continue
            if row["relation"] == "closes" and row["source"] == neighbour:
                links.append((str(neighbour), "closes"))
            elif row["relation"] == "references":
                links.append((str(neighbour), "references"))
    ranked = {node: relation for node, relation in sorted(links)}
    for node, relation in links:
        if relation == "closes":
            ranked[node] = "closes"
    return sorted(ranked.items())


def _blockers(graph: nx.Graph, issue: str) -> list[str]:
    blockers: list[str] = []
    for neighbour in graph.neighbors(issue):
        for row in _directed(graph, issue, neighbour):
            if row["relation"] != "blocked_by" or row["source"] != issue:
                continue
            if graph.nodes[neighbour].get("state") in ("open", "draft"):
                blockers.append(str(neighbour))
    return sorted(set(blockers))


def _assignees(graph: nx.Graph, issue: str) -> list[str]:
    assignees: list[str] = []
    for neighbour in graph.neighbors(issue):
        if graph.nodes[neighbour].get("type") != "CONTRIBUTOR":
            continue
        for row in _directed(graph, issue, neighbour):
            if (
                row.get("origin") == "github"
                and row.get("relation") == "assigned_to"
                and row.get("source") == issue
            ):
                assignees.append(str(neighbour))
    return sorted(set(assignees))


def _concepts(graph: nx.Graph, issue: str) -> list[str]:
    return sorted(
        str(neighbour)
        for neighbour in graph.neighbors(issue)
        if is_concept(graph.nodes[neighbour].get("type"))
    )


def score_issue(graph: nx.Graph, issue: str) -> Opportunity:
    data = graph.nodes[issue]
    state = data.get("state") or "open"
    labels = sorted(data.get("labels", []))
    url = data.get("url")
    title = data.get("description") or str(issue)

    evidence = [OpportunityEvidence(label=str(issue), url=url)]
    reasons: list[str] = []

    if state == "closed":
        return Opportunity(
            node=str(issue),
            number=int(data.get("number") or 0),
            title=title,
            url=url,
            state=state,
            status="closed",
            score=0.0,
            labels=labels,
            reasons=["issue is closed"],
            evidence=evidence,
        )

    concepts = _concepts(graph, issue)
    claims = _claim_links(graph, issue)
    assignees = _assignees(graph, issue)
    blockers = _blockers(graph, issue)
    locked = bool(data.get("locked", False))
    blocking_dependency_count = int(data.get("blocking_dependency_count", 0))

    score = BASE_SCORE
    reasons.append(f"open issue (+{BASE_SCORE:.2f})")

    matched_labels = sorted({label for label in labels if label.lower() in HELP_LABELS})
    if matched_labels:
        score += HELP_WANTED_BONUS
        reasons.append(f"labeled {', '.join(matched_labels)} (+{HELP_WANTED_BONUS:.2f})")

    if concepts:
        bonus = CONCEPT_BONUS * min(len(concepts), CONCEPT_CAP)
        score += bonus
        preview = ", ".join(concepts[:4])
        reasons.append(f"{len(concepts)} linked technical concepts: {preview} (+{bonus:.2f})")

    for blocker in blockers:
        evidence.append(
            OpportunityEvidence(label=f"blocked by {blocker}", url=graph.nodes[blocker].get("url"))
        )
    platform_blocks: list[str] = []
    if blockers:
        platform_blocks.append(f"blocked by open {', '.join(blockers)}")
    if locked:
        evidence.append(OpportunityEvidence(label="issue conversation is locked", url=url))
        platform_blocks.append("issue conversation is locked")
    if blocking_dependency_count:
        evidence.append(
            OpportunityEvidence(
                label=f"GitHub reports {blocking_dependency_count} blocking dependencies",
                url=url,
            )
        )
        platform_blocks.append(
            f"GitHub reports {blocking_dependency_count} blocking dependencies"
        )
    if platform_blocks:
        score -= BLOCKED_PENALTY
        reasons.append(f"{'; '.join(platform_blocks)} (-{BLOCKED_PENALTY:.2f})")

    claimed_by = [node for node, _ in claims]
    for assignee in assignees:
        evidence.append(OpportunityEvidence(label=f"assigned to {assignee}", url=url))
    for node, relation in claims:
        pr_state = graph.nodes[node].get("state")
        evidence.append(
            OpportunityEvidence(
                label=f"{node} ({pr_state}) {relation} this issue",
                url=graph.nodes[node].get("url"),
            )
        )
    if claims or assignees:
        score -= CLAIMED_PENALTY
        details: list[str] = []
        if assignees:
            details.append(f"assigned to {', '.join(assignees)}")
        if claims:
            pull_detail = ", ".join(
                f"{node} ({graph.nodes[node].get('state')})" for node, _ in claims
            )
            details.append(f"picked up by {pull_detail}")
        reasons.append(f"already {' and '.join(details)} (-{CLAIMED_PENALTY:.2f})")

    if claims or assignees:
        status = "claimed"
    elif platform_blocks:
        status = "blocked"
    else:
        status = "available"

    return Opportunity(
        node=str(issue),
        number=int(data.get("number") or 0),
        title=title,
        url=url,
        state=state,
        status=status,
        score=round(max(score, 0.0), 4),
        labels=labels,
        concepts=concepts,
        claimed_by=claimed_by,
        assignees=assignees,
        blocked_by=blockers,
        locked=locked,
        blocking_dependency_count=blocking_dependency_count,
        reasons=reasons,
        evidence=evidence,
    )


def opportunities(graph: nx.Graph, include_closed: bool = False) -> list[Opportunity]:
    """Rank open issues by how worthwhile they are to pick up right now."""
    scored = [
        score_issue(graph, str(node))
        for node, data in graph.nodes(data=True)
        if data.get("type") == "ISSUE"
    ]
    if not include_closed:
        scored = [item for item in scored if item.status != "closed"]
    return sorted(scored, key=lambda item: (-item.score, item.number))


def diff_opportunities(
    before: list[Opportunity],
    after: list[Opportunity],
) -> list[OpportunityChange]:
    """Explain how one event moved the recommendations."""
    before_by_node = {item.node: item for item in before}
    after_by_node = {item.node: item for item in after}
    changes: list[OpportunityChange] = []

    for node in sorted(set(after_by_node) - set(before_by_node)):
        item = after_by_node[node]
        changes.append(
            OpportunityChange(
                node=node,
                title=item.title,
                change="appeared",
                after_status=item.status,
                after_score=item.score,
                reasons=item.reasons,
            )
        )

    for node in sorted(set(before_by_node) - set(after_by_node)):
        item = before_by_node[node]
        changes.append(
            OpportunityChange(
                node=node,
                title=item.title,
                change="disappeared",
                before_status=item.status,
                before_score=item.score,
                reasons=["no longer an open contribution opportunity"],
            )
        )

    for node in sorted(set(before_by_node) & set(after_by_node)):
        old, new = before_by_node[node], after_by_node[node]
        if old.status == new.status and abs(old.score - new.score) < 1e-6:
            continue
        gained = [reason for reason in new.reasons if reason not in old.reasons]
        lost = [f"no longer: {reason}" for reason in old.reasons if reason not in new.reasons]
        changes.append(
            OpportunityChange(
                node=node,
                title=new.title,
                change="status_changed" if old.status != new.status else "score_changed",
                before_status=old.status,
                after_status=new.status,
                before_score=old.score,
                after_score=new.score,
                reasons=gained + lost,
            )
        )

    return changes
