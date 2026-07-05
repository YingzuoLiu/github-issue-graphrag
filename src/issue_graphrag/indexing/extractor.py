from __future__ import annotations

import json

from pydantic import ValidationError

from issue_graphrag.llm.client import LLMClient
from issue_graphrag.models import Entity, ExtractionResult, Relationship, TextUnit
from issue_graphrag.prompts import ENTITY_EXTRACTION_PROMPT


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().split())


def extract_from_text_unit(text_unit: TextUnit, llm: LLMClient) -> ExtractionResult:
    """Extract entities and relationships from a TextUnit using an LLM."""
    prompt = ENTITY_EXTRACTION_PROMPT.format(text=text_unit.text)
    raw = llm.complete(prompt)

    try:
        parsed = json.loads(raw)
        result = ExtractionResult.model_validate(parsed)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"LLM extraction output is not valid JSON: {raw}") from exc

    entities: list[Entity] = []
    for entity in result.entities:
        entity.name = _normalize_name(entity.name)
        entity.source_ids = sorted(set(entity.source_ids + [text_unit.id]))
        if entity.name:
            entities.append(entity)

    relationships: list[Relationship] = []
    for rel in result.relationships:
        rel.source = _normalize_name(rel.source)
        rel.target = _normalize_name(rel.target)
        rel.relation = rel.relation.strip().lower().replace(" ", "_")
        rel.source_ids = sorted(set(rel.source_ids + [text_unit.id]))
        if rel.source and rel.target and rel.source != rel.target:
            relationships.append(rel)

    return ExtractionResult(entities=entities, relationships=relationships)


def extract_all(text_units: list[TextUnit], llm: LLMClient) -> ExtractionResult:
    all_entities: list[Entity] = []
    all_relationships: list[Relationship] = []

    for text_unit in text_units:
        result = extract_from_text_unit(text_unit, llm)
        all_entities.extend(result.entities)
        all_relationships.extend(result.relationships)

    return ExtractionResult(entities=all_entities, relationships=all_relationships)
