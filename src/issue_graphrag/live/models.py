from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from issue_graphrag.live.timeutil import is_after, is_before_or_equal

FactKind = Literal["entity", "relation"]
FactOrigin = Literal["github", "llm"]

FactKey = tuple[str, str, str, str, str, str]

#: Node-level predicates. Everything else is projected as a graph edge.
ENTITY_PREDICATES = ("is_a", "has_state", "has_label")

#: Relations GitHub states explicitly. These are never produced by the LLM.
GITHUB_RELATIONS = ("references", "closes", "blocked_by", "touches", "belongs_to")


class Evidence(BaseModel):
    """Where a fact came from, so every edge in the graph stays traceable."""

    kind: str
    ref: str
    url: str | None = None
    snippet: str = ""
    text_unit_id: str | None = None


class Fact(BaseModel):
    """One versioned assertion about the repository.

    Facts are never deleted. When the underlying text or state stops supporting
    a fact it is closed with ``valid_to``, which keeps history queryable while
    removing the fact from the current graph projection.
    """

    kind: FactKind
    subject: str
    predicate: str
    object: str
    origin: FactOrigin
    document_id: str
    description: str = ""
    weight: float = 1.0
    evidence: list[Evidence] = Field(default_factory=list)
    observed_at: str
    valid_from: str
    valid_to: str | None = None
    first_delivery_id: str | None = None
    last_delivery_id: str | None = None
    invalidated_by: str | None = None

    @property
    def key(self) -> FactKey:
        """Stable identity of a fact, independent of when it was observed."""
        return (
            self.kind,
            self.document_id,
            self.origin,
            self.subject,
            self.predicate,
            self.object,
        )

    def is_valid_at(self, moment: str | None = None) -> bool:
        if moment is None:
            return self.valid_to is None
        if not is_before_or_equal(self.valid_from, moment):
            return False
        return self.valid_to is None or is_after(self.valid_to, moment)

    def label(self) -> str:
        return f"{self.subject} --{self.predicate}--> {self.object}"


class Comment(BaseModel):
    id: str
    author: str = ""
    body: str = ""
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class RepoItem(BaseModel):
    """An issue or pull request as currently known to the live index."""

    kind: Literal["issue", "pull_request"]
    repo: str
    number: int
    title: str = ""
    body: str = ""
    state: str = "open"
    merged: bool = False
    draft: bool = False
    labels: list[str] = Field(default_factory=list)
    author: str = ""
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    merged_at: str | None = None
    comments: dict[str, Comment] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)

    @property
    def document_id(self) -> str:
        slug = "issue" if self.kind == "issue" else "pull"
        return f"{self.repo}#{slug}-{self.number}"

    @property
    def node_name(self) -> str:
        prefix = "Issue" if self.kind == "issue" else "PR"
        return f"{prefix} #{self.number}"

    @property
    def document_title(self) -> str:
        prefix = "Issue" if self.kind == "issue" else "PR"
        return f"{prefix} #{self.number}: {self.title}"

    def lifecycle_state(self) -> str:
        """Single state string used for both graph projection and scoring."""
        if self.kind == "pull_request":
            if self.merged:
                return "merged"
            if self.state == "closed":
                return "closed"
            return "draft" if self.draft else "open"
        return "closed" if self.state == "closed" else "open"

    def ordered_comments(self) -> list[Comment]:
        return sorted(
            self.comments.values(),
            key=lambda c: (c.created_at or "", c.id),
        )

    def document_text(self) -> str:
        prefix = "Issue" if self.kind == "issue" else "Pull Request"
        parts = [f"{prefix} #{self.number}: {self.title}".strip(), self.body.strip()]
        if self.files:
            parts.append("Changed files:\n" + "\n".join(f"- {path}" for path in sorted(self.files)))
        for comment in self.ordered_comments():
            author = comment.author or "unknown"
            parts.append(f"Comment by {author}:\n{comment.body.strip()}")
        return "\n\n".join(part for part in parts if part).strip()

    def extraction_signature(self) -> str:
        """Hash of everything that would change LLM extraction output.

        State transitions such as close or merge deliberately do not appear
        here: they change deterministic facts and recommendations without
        requiring another extraction call.
        """
        payload = {
            "title": self.title,
            "body": self.body,
            "files": sorted(self.files),
            "comments": [
                {"id": c.id, "author": c.author, "body": c.body}
                for c in self.ordered_comments()
            ],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RepoEvent(BaseModel):
    """A normalized GitHub webhook delivery.

    ``attachments`` carries data a real handler fetches deterministically from
    the REST API alongside the hook, such as the file list of a pull request,
    which GitHub does not include in the webhook payload itself.
    """

    delivery_id: str
    event_type: str
    action: str
    repo: str
    received_at: str
    payload: dict[str, Any] = Field(default_factory=dict)
    attachments: dict[str, Any] = Field(default_factory=dict)

    def summary(self) -> str:
        return f"{self.event_type}.{self.action}"


class FactChange(BaseModel):
    change: Literal["added", "updated", "invalidated"]
    fact: Fact


class RejectedFact(BaseModel):
    """An inferred fact the ontology refused, kept for transparency."""

    fact: Fact
    reason: str


class OpportunityEvidence(BaseModel):
    label: str
    url: str | None = None


class Opportunity(BaseModel):
    """A scored contribution opportunity with the reasons behind the score."""

    node: str
    number: int
    title: str
    url: str | None = None
    state: str
    status: str
    score: float
    labels: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    claimed_by: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    evidence: list[OpportunityEvidence] = Field(default_factory=list)


class OpportunityChange(BaseModel):
    node: str
    title: str
    change: Literal["appeared", "disappeared", "status_changed", "score_changed"]
    before_status: str | None = None
    after_status: str | None = None
    before_score: float | None = None
    after_score: float | None = None
    reasons: list[str] = Field(default_factory=list)


class GraphDelta(BaseModel):
    """What one event did to the graph and to the recommendations."""

    delivery_id: str
    event_type: str
    action: str
    occurred_at: str
    repo: str
    applied: bool = True
    skip_reason: str | None = None
    affected_documents: list[str] = Field(default_factory=list)
    reextracted_documents: list[str] = Field(default_factory=list)
    fact_changes: list[FactChange] = Field(default_factory=list)
    rejected_inferred: list[RejectedFact] = Field(default_factory=list)
    added_nodes: list[str] = Field(default_factory=list)
    removed_nodes: list[str] = Field(default_factory=list)
    added_edges: list[tuple[str, str]] = Field(default_factory=list)
    removed_edges: list[tuple[str, str]] = Field(default_factory=list)
    changed_nodes: list[str] = Field(default_factory=list)
    opportunity_changes: list[OpportunityChange] = Field(default_factory=list)

    def changes_of(self, change: str) -> list[Fact]:
        return [item.fact for item in self.fact_changes if item.change == change]

    def is_noop(self) -> bool:
        return not self.fact_changes and not self.opportunity_changes

    def focus_nodes(self) -> list[str]:
        nodes: set[str] = set(self.added_nodes) | set(self.changed_nodes)
        for item in self.fact_changes:
            nodes.add(item.fact.subject)
            if item.fact.kind == "relation":
                nodes.add(item.fact.object)
        return sorted(nodes)


class LiveState(BaseModel):
    """Everything needed to project the graph at any point in its history."""

    version: str = "0.2"
    repo: str
    items: dict[str, RepoItem] = Field(default_factory=dict)
    facts: list[Fact] = Field(default_factory=list)
    processed_deliveries: list[str] = Field(default_factory=list)
    extraction_signatures: dict[str, str] = Field(default_factory=dict)
    last_event_at: str | None = None

    def valid_facts(self, moment: str | None = None) -> list[Fact]:
        facts = [fact for fact in self.facts if fact.is_valid_at(moment)]
        return sorted(facts, key=lambda fact: fact.key)

    def document_facts(self, document_id: str, origin: FactOrigin) -> list[Fact]:
        return [
            fact
            for fact in self.facts
            if fact.document_id == document_id
            and fact.origin == origin
            and fact.valid_to is None
        ]

    def has_delivery(self, delivery_id: str) -> bool:
        return delivery_id in set(self.processed_deliveries)
