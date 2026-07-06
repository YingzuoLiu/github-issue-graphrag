from issue_graphrag.indexing.normalizer import canonical_entity_name, normalize_extraction
from issue_graphrag.models import Entity, ExtractionResult, Relationship


def test_canonical_entity_name_aliases():
    assert canonical_entity_name("TG") == "TrustGraph"
    assert canonical_entity_name("Trust Graph") == "TrustGraph"
    assert canonical_entity_name("graph_rag") == "Graph RAG"
    assert canonical_entity_name("Graph-RAG") == "Graph RAG"
    assert canonical_entity_name("Reciprocal Rank Fusion") == "RRF"
    assert canonical_entity_name("rrf") == "RRF"


def test_canonical_entity_name_normalizes_issue_references():
    assert canonical_entity_name("#944") == "Issue #944"
    assert canonical_entity_name("issue 875") == "Issue #875"


def test_normalize_extraction_merges_alias_entities_and_rewrites_edges():
    result = ExtractionResult(
        entities=[
            Entity(name="TG", type="TOOL", description="short name", source_ids=["s1"]),
            Entity(name="TrustGraph", type="MODULE", description="full name", source_ids=["s2"]),
            Entity(name="graph_rag", type="FEATURE", description="graph retrieval", source_ids=["s1"]),
            Entity(name="Graph-RAG", type="FEATURE", description="same feature", source_ids=["s2"]),
            Entity(name="Reciprocal Rank Fusion", type="ALGORITHM", source_ids=["s3"]),
        ],
        relationships=[
            Relationship(
                source="TG",
                target="graph_rag",
                relation="uses",
                description="TrustGraph uses graph-rag",
                source_ids=["s1"],
            ),
            Relationship(
                source="Graph-RAG",
                target="Reciprocal Rank Fusion",
                relation="depends on",
                description="graph-rag can depend on ranking fusion",
                source_ids=["s2"],
            ),
        ],
    )

    normalized = normalize_extraction(result)

    names = {entity.name for entity in normalized.entities}
    assert "TrustGraph" in names
    assert "Graph RAG" in names
    assert "RRF" in names
    assert "TG" not in names
    assert "graph_rag" not in names
    assert "Graph-RAG" not in names

    trustgraph = next(entity for entity in normalized.entities if entity.name == "TrustGraph")
    assert trustgraph.source_ids == ["s1", "s2"]

    edges = {(rel.source, rel.target, rel.relation) for rel in normalized.relationships}
    assert ("TrustGraph", "Graph RAG", "uses") in edges
    assert ("Graph RAG", "RRF", "depends_on") in edges
