"""The schema the contribution graph is allowed to contain.

The live index is ontology-driven rather than free-form: node types, predicates,
their direction, their domain and range, and — crucially — which of them an LLM
is permitted to assert are declared here, in one place. Extraction output is
validated against this schema before it is allowed to become a fact.

The rule that matters most is the origin split. Predicates GitHub states
outright (state, labels, assignees, references, closing keywords, changed files) are
``github`` predicates and can only be written by the deterministic path. An
inferred fact that tries to claim one is rejected, so the model can never
overwrite something the platform already told us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from issue_graphrag.live.models import Fact

Origin = Literal["github", "llm"]

#: Node types the deterministic path assigns. Anything else is a concept.
GITHUB_NODE_TYPES = ("ISSUE", "PULL_REQUEST", "FILE", "MODULE", "CONTRIBUTOR")

#: Families used for domain and range checks.
ITEM_TYPES = ("ISSUE", "PULL_REQUEST")
CONCEPT = "CONCEPT"

#: Fallback predicate for an inferred relation outside the declared vocabulary.
FALLBACK_PREDICATE = "related_to"


@dataclass(frozen=True)
class NodeType:
    name: str
    origin: Origin
    description: str


@dataclass(frozen=True)
class Predicate:
    name: str
    kind: Literal["entity", "relation"]
    origin: Origin
    description: str
    domain: tuple[str, ...] = field(default=())
    range: tuple[str, ...] = field(default=())
    functional: bool = False

    def accepts(self, subject_type: str | None, object_type: str | None) -> bool:
        """Domain and range are only enforced for types we actually know."""
        if self.domain and subject_type and subject_type not in self.domain:
            return False
        if self.range and object_type and object_type not in self.range:
            return False
        return True


NODE_TYPES: dict[str, NodeType] = {
    "ISSUE": NodeType("ISSUE", "github", "A GitHub issue."),
    "PULL_REQUEST": NodeType("PULL_REQUEST", "github", "A GitHub pull request."),
    "FILE": NodeType("FILE", "github", "A file changed by a pull request."),
    "MODULE": NodeType("MODULE", "github", "A top-level directory grouping files."),
    "CONTRIBUTOR": NodeType(
        "CONTRIBUTOR", "github", "A GitHub account assigned to repository work."
    ),
    CONCEPT: NodeType(CONCEPT, "llm", "A technical concept extracted from discussion."),
}

ENTITY_PREDICATES: dict[str, Predicate] = {
    "is_a": Predicate(
        "is_a", "entity", "llm", "Assigns the node its type.", functional=True
    ),
    "has_state": Predicate(
        "has_state",
        "entity",
        "github",
        "Lifecycle state: open, closed, draft or merged.",
        domain=ITEM_TYPES,
        functional=True,
    ),
    "has_label": Predicate(
        "has_label", "entity", "github", "A label GitHub reports on the item.", domain=ITEM_TYPES
    ),
}

RELATION_PREDICATES: dict[str, Predicate] = {
    "references": Predicate(
        "references",
        "relation",
        "github",
        "An explicit #number mention.",
        domain=ITEM_TYPES,
        range=ITEM_TYPES,
    ),
    "closes": Predicate(
        "closes",
        "relation",
        "github",
        "A closing keyword links a pull request to an issue.",
        domain=("PULL_REQUEST",),
        range=("ISSUE",),
    ),
    "blocked_by": Predicate(
        "blocked_by",
        "relation",
        "github",
        "A stated blocking dependency.",
        domain=ITEM_TYPES,
        range=ITEM_TYPES,
    ),
    "touches": Predicate(
        "touches",
        "relation",
        "github",
        "A pull request changes a file.",
        domain=("PULL_REQUEST",),
        range=("FILE",),
    ),
    "belongs_to": Predicate(
        "belongs_to",
        "relation",
        "github",
        "A file lives in a module.",
        domain=("FILE",),
        range=("MODULE",),
    ),
    "assigned_to": Predicate(
        "assigned_to",
        "relation",
        "github",
        "GitHub currently assigns an issue or pull request to an account.",
        domain=ITEM_TYPES,
        range=("CONTRIBUTOR",),
    ),
    "implements": Predicate("implements", "relation", "llm", "Work realizes a proposal."),
    "conflicts_with": Predicate(
        "conflicts_with", "relation", "llm", "Two proposals cannot both hold."
    ),
    "supersedes": Predicate("supersedes", "relation", "llm", "A newer approach replaces an older."),
    "proposes": Predicate("proposes", "relation", "llm", "A discussion proposes an approach."),
    "affects": Predicate("affects", "relation", "llm", "A change has impact elsewhere."),
    "depends_on": Predicate("depends_on", "relation", "llm", "A technical dependency."),
    "uses": Predicate("uses", "relation", "llm", "One component uses another."),
    "used_by": Predicate("used_by", "relation", "llm", "Inverse of uses."),
    "improves": Predicate("improves", "relation", "llm", "One approach improves another."),
    "combines": Predicate("combines", "relation", "llm", "Two approaches are combined."),
    "implemented_in": Predicate("implemented_in", "relation", "llm", "A concept lives in a file."),
    "configures": Predicate("configures", "relation", "llm", "One component configures another."),
    "defines": Predicate("defines", "relation", "llm", "One component defines another."),
    "contains": Predicate("contains", "relation", "llm", "Structural containment."),
    "communicates_with": Predicate(
        "communicates_with", "relation", "llm", "Runtime communication path."
    ),
    "mentions": Predicate("mentions", "relation", "llm", "A weak textual association."),
    FALLBACK_PREDICATE: Predicate(
        FALLBACK_PREDICATE, "relation", "llm", "An inferred association outside the vocabulary."
    ),
}

PREDICATES: dict[str, Predicate] = {**ENTITY_PREDICATES, **RELATION_PREDICATES}

#: Predicates only the deterministic path may write.
GITHUB_ONLY = tuple(sorted(name for name, p in PREDICATES.items() if p.origin == "github"))


def predicate(name: str) -> Predicate | None:
    return PREDICATES.get(name)


def canonical_inferred_predicate(name: str) -> str:
    """Fold an inferred relation label into the declared vocabulary."""
    cleaned = (name or "").strip().lower().replace(" ", "_")
    known = RELATION_PREDICATES.get(cleaned)
    if known and known.origin == "llm":
        return cleaned
    return FALLBACK_PREDICATE


def node_types_from(facts: list[Fact]) -> dict[str, str]:
    """Resolve node types, letting deterministic types win over inferred ones."""
    types: dict[str, str] = {}
    for fact in sorted(facts, key=lambda item: item.key):
        if fact.kind != "entity" or fact.predicate != "is_a":
            continue
        current = types.get(fact.subject)
        if fact.origin == "github" and fact.object in GITHUB_NODE_TYPES:
            types[fact.subject] = fact.object
        elif current is None or current == CONCEPT:
            types[fact.subject] = fact.object or CONCEPT
    return types


def is_concept(node_type: str | None) -> bool:
    return node_type is not None and node_type not in GITHUB_NODE_TYPES


def permits(predicate_name: str, subject_type: str | None, object_type: str | None) -> bool:
    """Whether a relation is allowed between these node types.

    Enforced during projection rather than during storage, because node types
    are knowledge that grows: a name is only known to be a pull request once the
    index has seen it. Checking here keeps the rule a pure function of the
    current fact set, so a replay and a rebuild reach the same graph.
    """
    declared = RELATION_PREDICATES.get(predicate_name)
    if declared is None:
        return True
    return declared.accepts(subject_type, object_type)


def validate_inferred(facts: list[Fact]) -> tuple[list[Fact], list[tuple[Fact, str]]]:
    """Filter extraction output down to what an LLM is allowed to assert.

    Only time-invariant rules live here: an inferred fact may never claim a
    predicate GitHub owns, and an unrecognised relation label is folded into the
    declared vocabulary. Type-dependent rules are applied at projection time
    instead. Rejections are returned with their reason so a replay can report
    exactly what the model tried to assert and why it was refused.
    """
    kept: list[Fact] = []
    rejected: list[tuple[Fact, str]] = []

    for fact in facts:
        if fact.origin != "llm":
            kept.append(fact)
            continue

        if fact.kind == "entity":
            declared = ENTITY_PREDICATES.get(fact.predicate)
            if declared is None or declared.origin == "github":
                rejected.append((fact, f"'{fact.predicate}' is not an inferable entity predicate"))
                continue
            kept.append(fact)
            continue

        if fact.predicate in GITHUB_ONLY:
            rejected.append((fact, f"'{fact.predicate}' may only be asserted by GitHub payloads"))
            continue

        canonical = canonical_inferred_predicate(fact.predicate)
        if canonical != fact.predicate:
            fact = fact.model_copy(update={"predicate": canonical})
        kept.append(fact)

    return kept, rejected


def describe() -> dict[str, list[dict[str, object]]]:
    """Machine-readable schema, used by the CLI and the demo UI."""
    return {
        "node_types": [
            {"name": t.name, "origin": t.origin, "description": t.description}
            for t in NODE_TYPES.values()
        ],
        "predicates": [
            {
                "name": p.name,
                "kind": p.kind,
                "origin": p.origin,
                "domain": list(p.domain) or ["*"],
                "range": list(p.range) or ["*"],
                "functional": p.functional,
                "description": p.description,
            }
            for p in PREDICATES.values()
        ],
    }
