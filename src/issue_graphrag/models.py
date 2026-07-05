from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SourceDocument(BaseModel):
    """A raw source document loaded from GitHub or local files."""

    id: str
    title: str
    text: str
    source_type: str = "document"
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TextUnit(BaseModel):
    """A chunk used both for extraction and source grounding."""

    id: str
    document_id: str
    text: str
    order: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class Entity(BaseModel):
    """A graph node extracted from a TextUnit."""

    name: str
    type: str = "CONCEPT"
    description: str = ""
    source_ids: list[str] = Field(default_factory=list)


class Relationship(BaseModel):
    """A graph edge extracted from a TextUnit."""

    source: str
    target: str
    relation: str
    description: str = ""
    weight: float = 1.0
    source_ids: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """Structured output expected from the entity-relation extractor."""

    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)


class CommunityReport(BaseModel):
    """LLM-generated summary of a graph community."""

    id: str
    title: str
    summary: str
    entity_names: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    rating: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    """A retrieved item with a score and optional payload."""

    id: str
    score: float
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
