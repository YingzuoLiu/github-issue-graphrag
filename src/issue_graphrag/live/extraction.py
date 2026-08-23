from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from issue_graphrag.indexing.extractor import (
    EXTRACTION_RESPONSE_SCHEMA,
    extract_all,
    extraction_prompt,
    parse_extraction_result,
)
from issue_graphrag.indexing.normalizer import normalize_extraction
from issue_graphrag.live.facts import snippet, text_sources
from issue_graphrag.live.models import Evidence, Fact, RepoItem
from issue_graphrag.llm.client import CompletionMetadata
from issue_graphrag.models import Entity, ExtractionResult, Relationship, TextUnit


@dataclass(frozen=True)
class UnitExtraction:
    result: ExtractionResult
    metadata: CompletionMetadata


class ExtractionValidationError(ValueError):
    """A provider response was billable but failed the extraction schema."""

    def __init__(self, message: str, metadata: CompletionMetadata):
        super().__init__(message)
        self.metadata = metadata


class Extractor(Protocol):
    """Anything that can turn TextUnits into entities and relationships."""

    def extract(self, text_units: list[TextUnit]) -> ExtractionResult:
        """Return the entities and relationships supported by these TextUnits."""


class LLMExtractor:
    """Production extractor: one LLM call per TextUnit of an affected document."""

    def __init__(self, llm: Any):
        self.llm = llm
        self.calls = 0

    @property
    def requested_model(self) -> str:
        model = getattr(self.llm, "model", None)
        if not model:
            raise ValueError("operational extraction requires an explicit requested model")
        return str(model)

    def extract(self, text_units: list[TextUnit]) -> ExtractionResult:
        self.calls += len(text_units)
        return extract_all(text_units, self.llm)

    def extract_unit(self, text_unit: TextUnit, *, max_output_tokens: int) -> UnitExtraction:
        """Make one strict, auditable provider call for an operational batch."""
        complete_structured = getattr(self.llm, "complete_structured", None)
        if complete_structured is None:
            raise TypeError("operational extraction requires structured completion support")
        self.calls += 1
        response = complete_structured(
            extraction_prompt(text_unit),
            schema_name="github_issue_graph_extraction",
            schema=EXTRACTION_RESPONSE_SCHEMA,
            max_tokens=max_output_tokens,
            require_parameters=True,
        )
        try:
            result = parse_extraction_result(response.content, text_unit)
        except ValueError as exc:
            raise ExtractionValidationError(str(exc), response.metadata) from exc
        return UnitExtraction(result=result, metadata=response.metadata)


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
    result = extractor.extract(text_units)
    return llm_facts_from_result(
        item,
        text_units,
        result,
        moment,
        delivery_id,
    )


def llm_facts_from_result(
    item: RepoItem,
    text_units: list[TextUnit],
    result: ExtractionResult,
    moment: str,
    delivery_id: str | None = None,
) -> list[Fact]:
    """Wrap a complete, already validated document extraction as facts."""
    result = normalize_extraction(result)
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
                valid_from=moment,
                asserted_by=delivery_id,
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
                valid_from=moment,
                asserted_by=delivery_id,
            )
        )

    return facts
