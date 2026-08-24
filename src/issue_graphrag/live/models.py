from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from issue_graphrag.live.timeutil import is_after, is_before_or_equal

FactKind = Literal["entity", "relation"]
FactOrigin = Literal["github", "llm"]

FactKey = tuple[str, str, str, str, str, str]

#: Node-level predicates. Everything else is projected as a graph edge.
ENTITY_PREDICATES = (
    "is_a",
    "has_state",
    "has_label",
    "is_locked",
    "has_blocking_dependencies",
)

#: Relations GitHub states explicitly. These are never produced by the LLM.
GITHUB_RELATIONS = (
    "references",
    "closes",
    "blocked_by",
    "touches",
    "belongs_to",
    "assigned_to",
)


class Evidence(BaseModel):
    """Where a fact came from, so every edge in the graph stays traceable."""

    kind: str
    ref: str
    url: str | None = None
    snippet: str = ""
    text_unit_id: str | None = None


class Fact(BaseModel):
    """One immutable version of an assertion about the repository.

    A fact is written once and then only ever closed. Nothing rewrites its
    description, evidence or timestamps in place: if the payload behind a fact
    changes, the old version is closed with ``valid_to`` and a new version is
    appended. That is what makes a historical projection trustworthy — reading
    the graph at an earlier moment cannot surface evidence that was edited into
    existence later.

    Re-observing a fact that has not changed is deliberately a no-op. Counting
    observations would belong in a separate ledger; it must not touch the
    assertion, or an unrelated event elsewhere in the repository would make
    every fact look freshly confirmed.
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
    valid_from: str
    valid_to: str | None = None
    asserted_by: str | None = None
    invalidated_by: str | None = None

    def payload(self) -> tuple:
        """Everything that makes this a distinct *version* of the assertion."""
        evidence = tuple(
            sorted(
                (e.kind, e.ref, e.url or "", e.snippet, e.text_unit_id or "")
                for e in self.evidence
            )
        )
        return (self.description, round(self.weight, 6), evidence)

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
    source_delivery_id: str = ""

    def version_key(self) -> tuple[str, str]:
        return (self.updated_at or self.created_at or "", self.source_delivery_id)


class SourceVersion(BaseModel):
    """Deterministic source-side ordering for one field or tombstone."""

    effective_at: str = ""
    delivery_id: str = ""

    def key(self) -> tuple[str, str]:
        return (self.effective_at, self.delivery_id)


VERSIONED_ITEM_FIELDS = (
    "title",
    "body",
    "state",
    "draft",
    "merged",
    "merged_at",
    "labels",
    "assignees",
    "locked",
    "blocking_dependency_count",
    "author",
    "url",
    "created_at",
    "updated_at",
    "closed_at",
    "files",
)


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
    assignees: list[str] = Field(default_factory=list)
    locked: bool = False
    blocking_dependency_count: int = Field(default=0, ge=0)
    author: str = ""
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    merged_at: str | None = None
    comments: dict[str, Comment] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)

    #: Source-side version of this record: the newest timestamp GitHub reports
    #: for it. Distinct from ingestion time, so an out-of-order delivery cannot
    #: overwrite newer state with older state.
    effective_at: str | None = None
    #: Tie-breaker when two payloads claim the same effective_at.
    source_delivery_id: str = ""
    #: Per-field source versions. Webhook shapes are partial, so one record-wide
    #: stale guard is insufficient: a comment-shaped PR payload must not prevent
    #: an equally-timestamped pull_request payload from filling PR-only fields.
    field_versions: dict[str, SourceVersion] = Field(default_factory=dict)
    #: Comment id -> the source version that deleted it. Comparing the complete
    #: version prevents a stale delete from erasing a newer edit.
    deleted_comments: dict[str, SourceVersion] = Field(default_factory=dict)

    @field_validator("deleted_comments", mode="before")
    @classmethod
    def migrate_timestamp_tombstones(cls, value):  # noqa: ANN001 - Pydantic hook
        """Read states written before tombstones carried a delivery-id tie-break."""
        if not isinstance(value, dict):
            return value
        return {
            comment_id: (
                {"effective_at": tombstone, "delivery_id": ""}
                if isinstance(tombstone, str)
                else tombstone
            )
            for comment_id, tombstone in value.items()
        }

    def version_key(self) -> tuple[str, str]:
        return (self.effective_at or "", self.source_delivery_id)

    def seed_field_versions(self) -> None:
        """Mark unversioned snapshot fields as observed at the record version.

        ``setdefault`` also migrates states written before a newly introduced
        field existed without overwriting the independent versions already
        recorded for older fields.
        """
        version = SourceVersion(
            effective_at=self.effective_at or "",
            delivery_id=self.source_delivery_id,
        )
        for field in VERSIONED_ITEM_FIELDS:
            self.field_versions.setdefault(field, version.model_copy())

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
    #: Webhooks preserve GitHub's delivery chronology. Reconciliation events
    #: are current-state observations made by the scheduled synchronizer and
    #: must never be presented as if GitHub delivered them in real time.
    source: Literal["webhook", "reconciliation"] = "webhook"
    #: When the index actually applied this delivery. Fact validity windows are
    #: keyed on this, not on ``received_at``, so out-of-order arrivals cannot
    #: open a validity window in the past.
    indexed_at: str | None = None

    def summary(self) -> str:
        return f"{self.event_type}.{self.action}"

    def observation_label(self) -> str:
        if self.source == "reconciliation":
            return "Observed during scheduled sync"
        return "Received via GitHub Webhook"


class FactChange(BaseModel):
    """What happened to one assertion during an event.

    ``superseded`` closes the previous version of an assertion that is still
    true but whose evidence moved; the replacement is reported as ``updated``.
    """

    change: Literal["added", "updated", "invalidated", "superseded"]
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
    assignees: list[str] = Field(default_factory=list)
    blocked_by: list[str] = Field(default_factory=list)
    locked: bool = False
    blocking_dependency_count: int = 0
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
    indexed_at: str
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

    version: str = "0.3"
    repo: str
    items: dict[str, RepoItem] = Field(default_factory=dict)
    facts: list[Fact] = Field(default_factory=list)
    processed_deliveries: list[str] = Field(default_factory=list)
    extraction_signatures: dict[str, str] = Field(default_factory=dict)
    extraction_namespaces: dict[str, str] = Field(default_factory=dict)
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
