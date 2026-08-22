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


def reconcile_facts(
    state: LiveState,
    document_id: str,
    origin: FactOrigin,
    new_facts: list[Fact],
    moment: str,
    delivery_id: str | None = None,
) -> list[FactChange]:
    """Bring one document's facts of one origin up to date, append-only.

    Three outcomes, and no fourth:

    - the assertion is new             -> append an open version
    - the assertion no longer holds    -> close the open version
    - the payload behind it changed    -> close the old version, append a new one

    An assertion that is re-derived unchanged is left completely alone. Nothing
    in this function mutates a stored fact except to close it, which is what
    lets a historical projection be read without fear that later evidence has
    been backfilled into an earlier moment.
    """
    incoming = _dedupe(new_facts)
    current = {fact.key: fact for fact in state.document_facts(document_id, origin)}
    changes: list[FactChange] = []

    def open_version(fact: Fact, change: str) -> None:
        version = fact.model_copy(deep=True)
        version.valid_from = moment
        version.valid_to = None
        version.asserted_by = delivery_id
        version.invalidated_by = None
        state.facts.append(version)
        changes.append(FactChange(change=change, fact=version))

    def close_version(fact: Fact, change: str) -> None:
        fact.valid_to = moment
        fact.invalidated_by = delivery_id
        changes.append(FactChange(change=change, fact=fact))

    for key in sorted(set(incoming) - set(current)):
        open_version(incoming[key], "added")

    for key in sorted(set(current) - set(incoming)):
        close_version(current[key], "invalidated")

    for key in sorted(set(current) & set(incoming)):
        stored, fresh = current[key], incoming[key]
        if stored.payload() == fresh.payload():
            continue
        # The assertion still holds but its evidence or description moved, so
        # the old version is retired and a new one supersedes it.
        close_version(stored, "superseded")
        open_version(fresh, "updated")

    return changes


def write_state(path: Path, state: LiveState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state.model_dump(), handle, ensure_ascii=False, indent=2)


def read_state(path: Path) -> LiveState:
    with path.open("r", encoding="utf-8") as handle:
        state = LiveState.model_validate(json.load(handle))
    for item in state.items.values():
        # States written before per-field source versions were introduced came
        # from complete snapshots/records, so all of their fields share the
        # record-level version as the migration baseline.
        item.seed_field_versions()
    return state
