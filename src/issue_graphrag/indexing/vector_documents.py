from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from issue_graphrag.models import CommunityReport, Entity, TextUnit


@dataclass(frozen=True)
class VectorDocument:
    kind: str
    source_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


def text_unit_documents(text_units: list[TextUnit]) -> list[VectorDocument]:
    documents: list[VectorDocument] = []
    for unit in text_units:
        title = str(unit.metadata.get("document_title", ""))
        documents.append(
            VectorDocument(
                kind="text_unit",
                source_id=unit.id,
                text=f"{title}\n{unit.text}".strip(),
                metadata={
                    "document_id": unit.document_id,
                    "document_title": title,
                    "order": unit.order,
                    "source_type": unit.metadata.get("source_type"),
                    "url": unit.metadata.get("url"),
                },
            )
        )
    return documents


def entity_documents(entities: list[Entity]) -> list[VectorDocument]:
    return [
        VectorDocument(
            kind="entity",
            source_id=entity.name,
            text=f"{entity.name}\nType: {entity.type}\n{entity.description}".strip(),
            metadata={
                "entity_type": entity.type,
                "source_ids": entity.source_ids,
            },
        )
        for entity in entities
    ]


def community_report_documents(
    reports: list[CommunityReport],
) -> list[VectorDocument]:
    return [
        VectorDocument(
            kind="community_report",
            source_id=report.id,
            text=(
                f"{report.title}\nEntities: {', '.join(report.entity_names)}\n"
                f"{report.summary}"
            ).strip(),
            metadata={
                "rating": report.rating,
                "entity_names": report.entity_names,
                "source_ids": report.source_ids,
            },
        )
        for report in reports
    ]


def build_vector_documents(
    text_units: list[TextUnit],
    entities: list[Entity],
    reports: list[CommunityReport],
) -> list[VectorDocument]:
    return [
        *text_unit_documents(text_units),
        *entity_documents(entities),
        *community_report_documents(reports),
    ]
