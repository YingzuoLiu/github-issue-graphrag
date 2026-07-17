from issue_graphrag.indexing.vector_documents import build_vector_documents
from issue_graphrag.models import CommunityReport, Entity, TextUnit


def test_build_vector_documents_preserves_identity_and_grounding_metadata():
    text_unit = TextUnit(
        id="unit-1",
        document_id="repo#issue-1",
        text="The source evidence.",
        order=2,
        metadata={
            "document_title": "Issue #1",
            "source_type": "github_issue",
            "url": "https://example.com/1",
        },
    )
    entity = Entity(
        name="Graph RAG",
        type="FEATURE",
        description="Graph-aware retrieval",
        source_ids=["unit-1"],
    )
    report = CommunityReport(
        id="community-1",
        title="Retrieval",
        summary="Graph RAG retrieval work.",
        entity_names=["Graph RAG"],
        source_ids=["unit-1"],
        rating=8.5,
    )

    documents = build_vector_documents([text_unit], [entity], [report])

    assert [document.kind for document in documents] == [
        "text_unit",
        "entity",
        "community_report",
    ]
    assert documents[0].source_id == "unit-1"
    assert documents[0].metadata["document_id"] == "repo#issue-1"
    assert documents[1].source_id == "Graph RAG"
    assert documents[1].metadata["source_ids"] == ["unit-1"]
    assert documents[2].source_id == "community-1"
    assert documents[2].metadata["entity_names"] == ["Graph RAG"]
