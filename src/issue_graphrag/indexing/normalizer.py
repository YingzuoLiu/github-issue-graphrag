from __future__ import annotations

import re

from issue_graphrag.models import Entity, ExtractionResult, Relationship


ENTITY_ALIASES = {
    "tg": "TrustGraph",
    "trust graph": "TrustGraph",
    "trustgraph": "TrustGraph",
    "graph-rag": "Graph RAG",
    "graph rag": "Graph RAG",
    "graph_rag": "Graph RAG",

    "graph.rag": "Graph RAG",
    "graph rag processor": "Graph RAG",
    "graph_rag.processor": "Graph RAG",
    "graphrag": "Graph RAG",
    "document.rag": "Document RAG",
    "document_rag.py": "document_rag.py",
    "document-rag": "Document RAG",
    "trustgraph 2.3.21": "TrustGraph",
    "trustgraph community": "TrustGraph",
    "document-rag": "Document RAG",
    "document rag": "Document RAG",
    "document_rag": "Document RAG",
    "hybrid retrieval": "Hybrid Retrieval",
    "reciprocal rank fusion": "RRF",
    "rrf": "RRF",
    "bm25": "BM25",
    "tf-idf": "TF-IDF",
    "tfidf": "TF-IDF",
    "kafka": "Kafka",
    "pulsar": "Pulsar",
    "neo4j": "Neo4j",
    "memgraph": "Memgraph",
    "qdrant": "Qdrant",
    "cohere rerank": "Cohere Rerank API",
    "cohere rerank api": "Cohere Rerank API",
    "jina reranker": "Jina Reranker",
    "cross-encoder reranking": "Cross-encoder Reranking",
    "kafkabackendconsumer.unsubscribe()": "unsubscribe()",
}


RELATION_ALIASES = {
    "use": "uses",
    "uses": "uses",
    "used_by": "used_by",
    "depends on": "depends_on",
    "depends_on": "depends_on",
    "requires": "depends_on",
    "mentions": "mentions",
    "related_to": "mentions",
    "affect": "affects",
    "affects": "affects",
    "impacts": "affects",
    "improve": "improves",
    "improves": "improves",
    "propose": "proposes",
    "proposes": "proposes",
    "combine": "combines",
    "combines": "combines",
    "configures": "configures",
    "defines": "defines",
    "contains": "contains",
    "conflicts with": "conflicts_with",
    "conflicts_with": "conflicts_with",
    "communicates with": "communicates_with",
    "communicates_with": "communicates_with",
    "implements": "implements",
    "implemented_in": "implemented_in",
}


_FILE_LIKE_TYPES = {
    "FILE",
    "SCRIPT",
    "PROMPT_TEMPLATE",
}


def _clean_name(name: str) -> str:
    return " ".join((name or "").strip().split())


def _normalize_issue_name(name: str) -> str | None:
    match = re.match(r"^(?:issue\s*)?#?(\d+)$", name.strip(), re.IGNORECASE)
    if match:
        return f"Issue #{match.group(1)}"
    match = re.match(r"^issue\s+#?(\d+)$", name.strip(), re.IGNORECASE)
    if match:
        return f"Issue #{match.group(1)}"
    return None


def canonical_entity_name(name: str, entity_type: str | None = None) -> str:
    cleaned = _clean_name(name)
    if not cleaned:
        return cleaned

    issue_name = _normalize_issue_name(cleaned)
    if issue_name:
        return issue_name

    lowered = cleaned.lower().replace("_", " ").replace("-", "-")
    alias_key = lowered.strip()
    if alias_key in ENTITY_ALIASES:
        return ENTITY_ALIASES[alias_key]

    relaxed_key = lowered.replace("-", " ").strip()
    if relaxed_key in ENTITY_ALIASES:
        return ENTITY_ALIASES[relaxed_key]

    upper_type = (entity_type or "").upper()

    # Merge path-like file references to their basename for the MVP.
    # Example: trustgraph-base/trustgraph/base/kafka_backend.py -> kafka_backend.py
    if upper_type in _FILE_LIKE_TYPES and "/" in cleaned:
        tail = cleaned.split("/")[-1]
        if "." in tail:
            return tail

    return cleaned


def canonical_relation_label(label: str) -> str:
    cleaned = " ".join((label or "").strip().lower().replace("-", "_").split())
    cleaned = cleaned.replace(" ", "_")
    return RELATION_ALIASES.get(cleaned, cleaned)


def normalize_extraction(result: ExtractionResult) -> ExtractionResult:
    """Normalize entity names and relation labels, then merge duplicate entities."""

    observed_name_map: dict[str, str] = {}
    merged_entities: dict[str, Entity] = {}

    for entity in result.entities:
        canonical = canonical_entity_name(entity.name, entity.type)
        observed_name_map[entity.name] = canonical

        if canonical not in merged_entities:
            merged_entities[canonical] = Entity(
                name=canonical,
                type=entity.type,
                description=entity.description,
                source_ids=sorted(set(entity.source_ids)),
            )
            continue

        existing = merged_entities[canonical]
        existing.source_ids = sorted(set(existing.source_ids) | set(entity.source_ids))

        if not existing.description and entity.description:
            existing.description = entity.description

        # Prefer more specific type over generic concept.
        if existing.type == "CONCEPT" and entity.type != "CONCEPT":
            existing.type = entity.type

    normalized_relationships: list[Relationship] = []
    seen_edges: set[tuple[str, str, str, str]] = set()

    for rel in result.relationships:
        source = observed_name_map.get(rel.source, canonical_entity_name(rel.source))
        target = observed_name_map.get(rel.target, canonical_entity_name(rel.target))
        relation = canonical_relation_label(rel.relation)

        if not source or not target or source == target:
            continue

        key = (source, target, relation, rel.description)
        if key in seen_edges:
            continue
        seen_edges.add(key)

        normalized_relationships.append(
            Relationship(
                source=source,
                target=target,
                relation=relation,
                description=rel.description,
                weight=rel.weight,
                source_ids=sorted(set(rel.source_ids)),
            )
        )

    return ExtractionResult(
        entities=sorted(merged_entities.values(), key=lambda e: e.name.lower()),
        relationships=normalized_relationships,
    )
