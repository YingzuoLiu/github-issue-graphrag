"""Read-only presentation contract for the Contribution Radar.

The live index remains authoritative.  This module only projects its persisted
state into user-facing cards, details and freshness messages; it never calls a
provider and never writes repository state.  Keeping that boundary outside the
Streamlit script makes repo isolation and degraded behaviour executable tests
instead of UI conventions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.history import timeline
from issue_graphrag.live.models import Fact, LiveState, Opportunity, RepoEvent, RepoItem
from issue_graphrag.live.projection import project_graph
from issue_graphrag.live.repositories import (
    RepoFreshness,
    RepoPaths,
    canonical_repo,
    read_freshness,
)
from issue_graphrag.live.store import read_state

RadarOrigin = Literal["github", "inference"]
HistoryStatus = Literal["current", "not_started", "partial", "unavailable"]
CoverageStatus = Literal["bounded", "unknown", "unavailable"]

STATUS_LABELS = {
    "available": "Ready",
    "claimed": "Claimed",
    "blocked": "Blocked",
    "closed": "Closed",
}

_NUMBER = re.compile(r"#(\d+)$")


@dataclass(frozen=True)
class RadarEvidence:
    """One source shown to a user, retaining fact-vs-inference provenance."""

    origin: RadarOrigin
    label: str
    url: str | None
    snippet: str = ""
    kind: str = ""
    ref: str = ""

    @property
    def traceable(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class RadarReason:
    text: str
    origin: RadarOrigin
    evidence: tuple[RadarEvidence, ...] = ()

    @property
    def traceable(self) -> bool:
        return any(item.traceable for item in self.evidence)


@dataclass(frozen=True)
class RadarFactRow:
    label: str
    value: str
    evidence: tuple[RadarEvidence, ...] = ()


@dataclass(frozen=True)
class RadarInference:
    description: str
    relation: str
    evidence: tuple[RadarEvidence, ...]


@dataclass(frozen=True)
class RadarPullRequest:
    number: int
    title: str
    state: str
    url: str | None
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class RadarChange:
    number: int
    title: str
    change: str
    before_status: str | None
    after_status: str | None
    before_score: float | None
    after_score: float | None
    reasons: tuple[RadarReason, ...]
    observed_at: str
    indexed_at: str
    source_label: str
    delivery_id: str

    @property
    def before_label(self) -> str | None:
        return STATUS_LABELS.get(self.before_status or "")

    @property
    def after_label(self) -> str | None:
        return STATUS_LABELS.get(self.after_status or "")


@dataclass(frozen=True)
class RadarIssue:
    number: int
    node: str
    title: str
    url: str | None
    status: str
    score: float
    labels: tuple[str, ...]
    concepts: tuple[str, ...]
    assignees: tuple[str, ...]
    claimed_by: tuple[str, ...]
    blocked_by: tuple[str, ...]
    locked: bool
    blocking_dependency_count: int
    updated_at: str | None
    reasons: tuple[RadarReason, ...]
    github_evidence: tuple[RadarEvidence, ...]
    inferred_evidence: tuple[RadarEvidence, ...]
    github_facts: tuple[RadarFactRow, ...]
    inferred_context: tuple[RadarInference, ...]
    pull_requests: tuple[RadarPullRequest, ...]
    recent_changes: tuple[RadarChange, ...] = ()

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status.title())

    @property
    def evidence_complete(self) -> bool:
        return bool(self.github_evidence) and all(reason.traceable for reason in self.reasons)


@dataclass(frozen=True)
class RadarCoverage:
    status: CoverageStatus
    message: str
    item_limit: int | None = None
    comment_limit_per_item: int | None = None
    file_limit_per_pull: int | None = None
    observed_items: int | None = None
    cap_reached: bool = False


@dataclass(frozen=True)
class RadarHistory:
    status: HistoryStatus
    events: tuple[RepoEvent, ...] = ()
    message: str | None = None


@dataclass(frozen=True)
class RadarSnapshot:
    repo: str
    issues: tuple[RadarIssue, ...]
    recent_changes: tuple[RadarChange, ...]
    freshness: RepoFreshness
    coverage: RadarCoverage
    history_status: HistoryStatus
    history_message: str | None = None

    @property
    def source_current_is_confirmed(self) -> bool:
        return (
            self.freshness.source_status == "current"
            and self.freshness.source_kind is not None
            and self.freshness.last_source_sync_at is not None
        )

    @property
    def semantic_current_is_confirmed(self) -> bool:
        return (
            self.freshness.semantic_status == "current"
            and self.freshness.semantic_updated_at is not None
        )

    @property
    def opportunities(self) -> tuple[RadarIssue, ...]:
        return tuple(item for item in self.issues if item.status != "closed")

    def issue(self, number: int) -> RadarIssue | None:
        return next((item for item in self.issues if item.number == number), None)

    def count(self, status: str) -> int:
        return sum(item.status == status for item in self.opportunities)


def _dedupe_evidence(rows: list[RadarEvidence]) -> tuple[RadarEvidence, ...]:
    seen: set[tuple[str, str, str, str, str, str]] = set()
    result: list[RadarEvidence] = []
    for row in rows:
        key = (
            row.origin,
            row.label,
            row.url or "",
            row.snippet,
            row.kind,
            row.ref,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return tuple(result)


def _fact_evidence(fact: Fact) -> tuple[RadarEvidence, ...]:
    origin: RadarOrigin = "github" if fact.origin == "github" else "inference"
    rows = [
        RadarEvidence(
            origin=origin,
            label=evidence.ref or fact.label(),
            url=evidence.url,
            snippet=evidence.snippet,
            kind=evidence.kind,
            ref=evidence.ref,
        )
        for evidence in fact.evidence
    ]
    return _dedupe_evidence(rows)


def _issue_facts(state: LiveState, issue: Opportunity) -> tuple[list[Fact], list[Fact]]:
    item_document_id = f"{state.repo}#issue-{issue.number}"
    selected = [
        fact
        for fact in state.valid_facts()
        if fact.document_id == item_document_id
        or fact.subject == issue.node
        or (fact.kind == "relation" and fact.object == issue.node)
    ]
    github = [fact for fact in selected if fact.origin == "github"]
    inferred = [fact for fact in selected if fact.origin == "llm"]
    return github, inferred


def _item_for_number(state: LiveState, number: int, kind: str) -> RepoItem | None:
    return next(
        (item for item in state.items.values() if item.number == number and item.kind == kind),
        None,
    )


def _node_number(node: str) -> int | None:
    match = _NUMBER.search(node)
    return int(match.group(1)) if match else None


def _recommendation_evidence(
    opportunity: Opportunity,
    inferred: list[Fact],
) -> tuple[tuple[RadarEvidence, ...], tuple[RadarEvidence, ...]]:
    github = _dedupe_evidence(
        [
            RadarEvidence(
                origin="github",
                label=evidence.label,
                url=evidence.url,
                kind="recommendation",
                ref=opportunity.node,
            )
            for evidence in opportunity.evidence
        ]
    )
    inference_rows = [row for fact in inferred for row in _fact_evidence(fact)]
    return github, _dedupe_evidence(inference_rows)


def _reason_evidence(
    reason: str,
    github: tuple[RadarEvidence, ...],
    inferred: tuple[RadarEvidence, ...],
) -> tuple[tuple[RadarEvidence, ...], RadarOrigin]:
    if "linked technical concepts" in reason:
        return inferred, "inference"

    lowered = reason.casefold()
    if "picked up by" in lowered:
        matched = tuple(row for row in github if row.label.startswith("PR #"))
    elif "assigned to" in lowered:
        matched = tuple(row for row in github if row.label.startswith("assigned to"))
    elif "blocked by" in lowered:
        matched = tuple(row for row in github if row.label.startswith("blocked by"))
    elif "blocking dependencies" in lowered:
        matched = tuple(row for row in github if "blocking dependencies" in row.label)
    else:
        matched = github[:1]
    return matched or github[:1], "github"


def _github_fact_rows(
    item: RepoItem,
    opportunity: Opportunity,
    github_evidence: tuple[RadarEvidence, ...],
) -> tuple[RadarFactRow, ...]:
    issue_source = github_evidence[:1]

    def evidence_where(prefix: str) -> tuple[RadarEvidence, ...]:
        matched = tuple(row for row in github_evidence if row.label.startswith(prefix))
        return matched or issue_source

    rows = [
        RadarFactRow("State", item.lifecycle_state().title(), issue_source),
        RadarFactRow("Labels", ", ".join(item.labels) or "None", issue_source),
        RadarFactRow("Assignees", ", ".join(opportunity.assignees) or "Unassigned", issue_source),
        RadarFactRow(
            "Claiming / related PRs",
            ", ".join(opportunity.claimed_by) or "None",
            tuple(
                row
                for row in github_evidence
                if row.label.startswith("PR #")
            )
            or issue_source,
        ),
        RadarFactRow(
            "Explicit blockers",
            ", ".join(opportunity.blocked_by) or "None",
            evidence_where("blocked by"),
        ),
        RadarFactRow("Conversation locked", "Yes" if item.locked else "No", issue_source),
        RadarFactRow(
            "Native blocking dependencies",
            str(item.blocking_dependency_count),
            evidence_where("GitHub reports"),
        ),
        RadarFactRow("GitHub updated", item.updated_at or "Not recorded", issue_source),
    ]
    return tuple(rows)


def _inferred_context(facts: list[Fact]) -> tuple[RadarInference, ...]:
    rows: list[RadarInference] = []
    seen: set[tuple[str, str, str]] = set()
    for fact in sorted(facts, key=lambda row: row.key):
        evidence = _fact_evidence(fact)
        if not evidence:
            continue
        description = fact.description or fact.label()
        key = (description, fact.predicate, fact.document_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            RadarInference(
                description=description,
                relation=fact.predicate,
                evidence=evidence,
            )
        )
    return tuple(rows)


def _pull_requests(state: LiveState, opportunity: Opportunity) -> tuple[RadarPullRequest, ...]:
    rows: list[RadarPullRequest] = []
    for node in opportunity.claimed_by:
        number = _node_number(node)
        if number is None:
            continue
        item = _item_for_number(state, number, "pull_request")
        if item is None:
            continue
        rows.append(
            RadarPullRequest(
                number=number,
                title=item.title,
                state=item.lifecycle_state(),
                url=item.url,
                files=tuple(sorted(item.files)),
            )
        )
    return tuple(rows)


def _recent_changes(
    state: LiveState,
    events: tuple[RepoEvent, ...],
    ranked: list[Opportunity],
) -> tuple[RadarChange, ...]:
    changes: list[RadarChange] = []
    seen: set[int] = set()
    opportunities_by_number = {item.number: item for item in ranked}
    for view in reversed(timeline(state, list(events))):
        for change in reversed(view.delta.opportunity_changes):
            number = _node_number(change.node)
            if number is None or number in seen:
                continue
            opportunity = opportunities_by_number.get(number)
            if opportunity is None:
                continue
            _, inferred_facts = _issue_facts(state, opportunity)
            github_evidence, inferred_evidence = _recommendation_evidence(
                opportunity,
                inferred_facts,
            )
            reasons: list[RadarReason] = []
            for reason in change.reasons:
                evidence, origin = _reason_evidence(
                    reason,
                    github_evidence,
                    inferred_evidence,
                )
                reasons.append(RadarReason(text=reason, origin=origin, evidence=evidence))
            seen.add(number)
            changes.append(
                RadarChange(
                    number=number,
                    title=change.title,
                    change=change.change,
                    before_status=change.before_status,
                    after_status=change.after_status,
                    before_score=change.before_score,
                    after_score=change.after_score,
                    reasons=tuple(reasons),
                    observed_at=view.event.received_at,
                    indexed_at=view.after_moment,
                    source_label=view.event.observation_label(),
                    delivery_id=view.event.delivery_id,
                )
            )
            if len(changes) >= 12:
                return tuple(changes)
    return tuple(changes)


def _build_issue(
    state: LiveState,
    opportunity: Opportunity,
    changes: tuple[RadarChange, ...],
) -> RadarIssue:
    item = _item_for_number(state, opportunity.number, "issue")
    if item is None:
        raise ValueError(f"projected {opportunity.node} has no issue record")
    _, inferred_facts = _issue_facts(state, opportunity)
    github_evidence, inferred_evidence = _recommendation_evidence(
        opportunity,
        inferred_facts,
    )
    reasons: list[RadarReason] = []
    for reason in opportunity.reasons:
        evidence, origin = _reason_evidence(reason, github_evidence, inferred_evidence)
        reasons.append(RadarReason(text=reason, origin=origin, evidence=evidence))

    return RadarIssue(
        number=opportunity.number,
        node=opportunity.node,
        title=opportunity.title,
        url=opportunity.url,
        status=opportunity.status,
        score=opportunity.score,
        labels=tuple(opportunity.labels),
        concepts=tuple(opportunity.concepts),
        assignees=tuple(opportunity.assignees),
        claimed_by=tuple(opportunity.claimed_by),
        blocked_by=tuple(opportunity.blocked_by),
        locked=opportunity.locked,
        blocking_dependency_count=opportunity.blocking_dependency_count,
        updated_at=item.updated_at,
        reasons=tuple(reasons),
        github_evidence=github_evidence,
        inferred_evidence=inferred_evidence,
        github_facts=_github_fact_rows(item, opportunity, github_evidence),
        inferred_context=_inferred_context(inferred_facts),
        pull_requests=_pull_requests(state, opportunity),
        recent_changes=tuple(change for change in changes if change.number == opportunity.number),
    )


def read_event_snapshot(path: Path, repo: str) -> RadarHistory:
    """Read complete JSONL records without repairing or otherwise writing the log."""
    normalized = canonical_repo(repo)
    if not path.exists():
        return RadarHistory(status="not_started", message="No event history has been recorded yet.")

    try:
        payload = path.read_bytes()
    except OSError as exc:
        return RadarHistory(
            status="unavailable",
            message=f"Event history could not be read ({type(exc).__name__}).",
        )

    partial_tail = bool(payload) and not payload.endswith(b"\n")
    complete = payload.rpartition(b"\n")[0] if partial_tail else payload
    events: list[RepoEvent] = []
    try:
        for raw_line in complete.splitlines():
            if not raw_line.strip():
                continue
            event = RepoEvent.model_validate(json.loads(raw_line))
            if canonical_repo(event.repo) != normalized:
                raise ValueError(
                    f"event {event.delivery_id!r} belongs to {event.repo!r}, not {normalized!r}"
                )
            events.append(event)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return RadarHistory(
            status="unavailable",
            message=f"Event history is invalid ({type(exc).__name__}); current facts remain usable.",
        )

    if partial_tail:
        return RadarHistory(
            status="partial",
            events=tuple(events),
            message="A partial final event was ignored; current facts remain usable.",
        )
    return RadarHistory(status="current", events=tuple(events))


def read_coverage(path: Path) -> RadarCoverage:
    """Read the recorded bootstrap bounds; absence never implies complete coverage."""
    if not path.exists():
        return RadarCoverage(
            status="unknown",
            message="Coverage bounds are not recorded; repository completeness cannot be confirmed.",
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("bootstrap seed must be an object")
        backfill = payload.get("backfill")
        if not isinstance(backfill, dict):
            return RadarCoverage(
                status="unknown",
                message="This index predates recorded coverage bounds; completeness is unknown.",
            )
        item_limit = int(backfill["item_limit"])
        comment_limit = int(backfill["comment_limit_per_item"])
        file_limit = int(backfill["file_limit_per_pull"])
        items = payload.get("items")
        observed_items = len(items) if isinstance(items, list) else None
        cap_reached = observed_items is not None and observed_items >= item_limit
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        return RadarCoverage(
            status="unavailable",
            message=f"Coverage metadata is unavailable ({type(exc).__name__}); results may be partial.",
        )

    cap_note = " The item cap was reached, so older items may be absent." if cap_reached else ""
    return RadarCoverage(
        status="bounded",
        message=(
            f"Bounded index: up to {item_limit} items, {comment_limit} comments per item, "
            f"and {file_limit} files per pull request.{cap_note}"
        ),
        item_limit=item_limit,
        comment_limit_per_item=comment_limit,
        file_limit_per_pull=file_limit,
        observed_items=observed_items,
        cap_reached=cap_reached,
    )


def build_radar_snapshot(
    state: LiveState,
    history: RadarHistory,
    freshness: RepoFreshness,
    coverage: RadarCoverage,
) -> RadarSnapshot:
    repo = canonical_repo(state.repo)
    if canonical_repo(freshness.repo) != repo:
        raise ValueError(f"freshness belongs to {freshness.repo!r}, not {repo!r}")

    graph = project_graph(state)
    ranked = opportunities(graph, include_closed=True)
    changes = _recent_changes(state, history.events, ranked)
    issues = tuple(_build_issue(state, item, changes) for item in ranked)
    return RadarSnapshot(
        repo=repo,
        issues=issues,
        recent_changes=changes,
        freshness=freshness,
        coverage=coverage,
        history_status=history.status,
        history_message=history.message,
    )


def load_radar_snapshot(paths: RepoPaths) -> RadarSnapshot:
    """Load one selected repository only; every cross-repo mismatch fails closed."""
    state = read_state(paths.state)
    if canonical_repo(state.repo) != paths.repo:
        raise ValueError(f"state belongs to {state.repo!r}, not {paths.repo!r}")
    history = read_event_snapshot(paths.event_log, paths.repo)
    freshness = read_freshness(paths.freshness, paths.repo)
    coverage = read_coverage(paths.bootstrap_seed)
    return build_radar_snapshot(state, history, freshness, coverage)
