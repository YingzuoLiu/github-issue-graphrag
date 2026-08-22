from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from issue_graphrag.indexing.extractor import extract_all
from issue_graphrag.indexing.normalizer import normalize_extraction
from issue_graphrag.live.facts import snippet, text_sources
from issue_graphrag.live.models import Evidence, Fact, RepoItem
from issue_graphrag.models import Entity, ExtractionResult, Relationship, TextUnit


class Extractor(Protocol):
    """Anything that can turn TextUnits into entities and relationships."""

    def extract(self, text_units: list[TextUnit]) -> ExtractionResult:
        """Return the entities and relationships supported by these TextUnits."""


class LLMExtractor:
    """Production extractor: one LLM call per TextUnit of an affected document."""

    def __init__(self, llm: Any):
        self.llm = llm
        self.calls = 0

    def extract(self, text_units: list[TextUnit]) -> ExtractionResult:
        self.calls += len(text_units)
        return extract_all(text_units, self.llm)


class FixtureExtractor:
    """Deterministic offline stand-in for LLM extraction.

    Rules are hand-authored substring triggers, not recorded model output. They
    exist so the incremental pipeline, its provenance and its invalidation
    behaviour can be replayed and tested without an API key.
    """

    def __init__(self, rules: list[dict[str, Any]]):
        self.rules = rules
        self.calls = 0

    @classmethod
    def from_path(cls, path: Path) -> "FixtureExtractor":
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        rules = payload.get("rules", payload) if isinstance(payload, dict) else payload
        return cls(list(rules))

    def extract(self, text_units: list[TextUnit]) -> ExtractionResult:
        entities: list[Entity] = []
        relationships: list[Relationship] = []

        for unit in text_units:
            self.calls += 1
            lowered = unit.text.lower()
            for rule in self.rules:
                trigger = str(rule.get("match", "")).lower()
                if not trigger or trigger not in lowered:
                    continue

                for entity in rule.get("entities", []):
                    entities.append(Entity(**{**entity, "source_ids": [unit.id]}))
                for relation in rule.get("relationships", []):
                    relationships.append(Relationship(**{**relation, "source_ids": [unit.id]}))

        return ExtractionResult(entities=entities, relationships=relationships)


def _text_evidence(item: RepoItem, terms: list[str]) -> list[Evidence]:
    """Point an inferred fact at the issue body or comment that mentions it."""
    found: list[Evidence] = []
    for kind, ref, text, url in text_sources(item):
        if not text:
            continue
        lowered = text.lower()
        for term in terms:
            if not term:
                continue
            index = lowered.find(term.lower())
            if index < 0:
                continue
            found.append(
                Evidence(
                    kind=kind,
                    ref=ref,
                    url=url,
                    snippet=snippet(text, index, index + len(term)),
                )
            )
            break
    return found


def _unit_evidence(
    item: RepoItem,
    source_ids: list[str],
    units_by_id: dict[str, TextUnit],
) -> list[Evidence]:
    evidence: list[Evidence] = []
    for source_id in sorted(set(source_ids)):
        unit = units_by_id.get(source_id)
        if unit is None:
            continue
        evidence.append(
            Evidence(
                kind="text_unit",
                ref=source_id,
                url=item.url,
                snippet=" ".join(unit.text.split())[:200],
                text_unit_id=source_id,
            )
        )
    return evidence


def llm_facts_for_item(
    item: RepoItem,
    text_units: list[TextUnit],
    extractor: Extractor,
    moment: str,
    delivery_id: str | None = None,
) -> list[Fact]:
    """Run scoped extraction for one document and wrap the output as facts.

    Every returned fact carries the TextUnit it came from plus, where the term
    is locatable, the body or comment that mentions it. An inferred fact without
    evidence is not stored.
    """
    result = normalize_extraction(extractor.extract(text_units))
    units_by_id = {unit.id: unit for unit in text_units}

    facts: list[Fact] = []

    for entity in result.entities:
        name = entity.name
        evidence = _unit_evidence(item, entity.source_ids, units_by_id)
        evidence.extend(_text_evidence(item, [entity.name]))
        if not evidence:
            continue
        facts.append(
            Fact(
                kind="entity",
                subject=name,
                predicate="is_a",
                object=entity.type or "CONCEPT",
                origin="llm",
                document_id=item.document_id,
                description=entity.description,
                evidence=evidence,
                observed_at=moment,
                valid_from=moment,
                first_delivery_id=delivery_id,
                last_delivery_id=delivery_id,
            )
        )

    for relation in result.relationships:
        source, target = relation.source, relation.target
        if source == target:
            continue
        evidence = _unit_evidence(item, relation.source_ids, units_by_id)
        evidence.extend(_text_evidence(item, [relation.source, relation.target]))
        if not evidence:
            continue
        facts.append(
            Fact(
                kind="relation",
                subject=source,
                predicate=relation.relation,
                object=target,
                origin="llm",
                document_id=item.document_id,
                description=relation.description,
                weight=relation.weight,
                evidence=evidence,
                observed_at=moment,
                valid_from=moment,
                first_delivery_id=delivery_id,
                last_delivery_id=delivery_id,
            )
        )

    return facts
