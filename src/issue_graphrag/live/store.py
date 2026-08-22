from __future__ import annotations

import json
from pathlib import Path

from issue_graphrag.live.models import Fact, FactChange, FactOrigin, LiveState


def _dedupe(facts: list[Fact]) -> dict[tuple, Fact]:
    """Collapse facts that share an identity, merging their evidence."""
    merged: dict[tuple, Fact] = {}
    for fact in facts:
        existing = merged.get(fact.key)
        if existing is None:
            merged[fact.key] = fact.model_copy(deep=True)
            continue
        seen = {(e.kind, e.ref, e.snippet) for e in existing.evidence}
        for item in fact.evidence:
            if (item.kind, item.ref, item.snippet) not in seen:
                existing.evidence.append(item)
                seen.add((item.kind, item.ref, item.snippet))
        if not existing.description:
            existing.description = fact.description
        existing.weight = max(existing.weight, fact.weight)
    return merged


def _payload(fact: Fact) -> tuple:
    evidence = tuple(
        sorted((e.kind, e.ref, e.url or "", e.snippet, e.text_unit_id or "") for e in fact.evidence)
    )
    return (fact.description, round(fact.weight, 6), evidence)


def reconcile_facts(
    state: LiveState,
    document_id: str,
    origin: FactOrigin,
    new_facts: list[Fact],
    moment: str,
    delivery_id: str | None = None,
) -> list[FactChange]:
    """Replace one document's facts of one origin, closing what no longer holds.

    Facts are never removed from the state. A fact that disappears is closed
    with ``valid_to`` so the current graph loses it while history keeps it, and
    a fact that survives keeps its original ``valid_from`` so the graph can say
    how long something has been true.
    """
    incoming = _dedupe(new_facts)
    current = {fact.key: fact for fact in state.document_facts(document_id, origin)}
    changes: list[FactChange] = []

    for key in sorted(set(incoming) - set(current)):
        fact = incoming[key]
        fact.observed_at = moment
        fact.valid_from = moment
        fact.valid_to = None
        fact.first_delivery_id = delivery_id
        fact.last_delivery_id = delivery_id
        state.facts.append(fact)
        changes.append(FactChange(change="added", fact=fact))

    for key in sorted(set(current) - set(incoming)):
        fact = current[key]
        fact.valid_to = moment
        fact.invalidated_by = delivery_id
        changes.append(FactChange(change="invalidated", fact=fact))

    for key in sorted(set(current) & set(incoming)):
        stored, fresh = current[key], incoming[key]
        stored.last_delivery_id = delivery_id
        stored.observed_at = moment
        if _payload(stored) == _payload(fresh):
            continue
        stored.description = fresh.description or stored.description
        stored.weight = fresh.weight
        stored.evidence = fresh.evidence
        changes.append(FactChange(change="updated", fact=stored))

    return changes


def write_state(path: Path, state: LiveState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state.model_dump(), handle, ensure_ascii=False, indent=2)


def read_state(path: Path) -> LiveState:
    with path.open("r", encoding="utf-8") as handle:
        return LiveState.model_validate(json.load(handle))
