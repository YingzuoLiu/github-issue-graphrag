import json
from pathlib import Path

import networkx as nx
import pytest

from issue_graphrag.evaluation import (
    EvalCase,
    contains_term,
    evaluate_case,
    load_eval_cases,
    modes_for_case,
    recall_at_k,
    reciprocal_rank,
    summarize,
    trace_retrieval,
)
from issue_graphrag.models import CommunityReport, SearchResult, TextUnit


@pytest.fixture
def retrieval_data():
    graph = nx.Graph()
    graph.add_node("Kafka", type="COMPONENT", description="message backend")
    graph.add_node("Consumer", type="CLASS", description="blocking consumer")
    graph.add_edge(
        "Kafka",
        "Consumer",
        relations=["uses"],
        descriptions=["Kafka backend uses a Consumer"],
        source_ids=["unit-kafka"],
    )
    units = [
        TextUnit(
            id="unit-kafka",
            document_id="repo#issue-944",
            text="Kafka backend consumers can block after unsubscribe().",
            order=0,
            metadata={"document_title": "Issue #944: Kafka backend"},
        ),
        TextUnit(
            id="unit-vector",
            document_id="repo#issue-875",
            text="Hybrid retrieval combines BM25 and vector search.",
            order=0,
            metadata={"document_title": "Issue #875: Hybrid retrieval"},
        ),
    ]
    reports = [
        CommunityReport(
            id="community-kafka",
            title="Kafka reliability",
            summary="Kafka Consumer reliability and blocking behavior.",
            entity_names=["Kafka", "Consumer"],
            source_ids=["unit-kafka"],
            rating=8,
        ),
        CommunityReport(
            id="community-retrieval",
            title="Retrieval",
            summary="Hybrid Retrieval with BM25 and vector search.",
            entity_names=["Hybrid Retrieval", "BM25"],
            source_ids=["unit-vector"],
            rating=7,
        ),
    ]
    return graph, units, reports


def _fake_retrieve(mode, _question, _graph, _units, _reports, _top_k):
    if mode == "naive":
        return [
            SearchResult(id="unit-vector", score=2.0, text="[unit-vector] Hybrid retrieval"),
            SearchResult(id="unit-kafka", score=1.0, text="[unit-kafka] Kafka Consumer"),
        ]
    if mode == "local":
        context = "\n".join(
            [
                "-----Reports-----",
                "[community-kafka] Kafka reliability",
                "-----Entities-----",
                "- Kafka",
                "- Consumer",
                "-----Relationships-----",
                "- Kafka -- uses -- Consumer",
                "-----Sources-----",
                "[unit-kafka] Issue #944: Kafka backend",
            ]
        )
        return [SearchResult(id="local_context", score=1.0, text=context)]
    if mode == "global":
        context = "\n\n".join(
            [
                "[community-retrieval] Retrieval\nHybrid Retrieval BM25",
                "[community-kafka] Kafka reliability\nKafka Consumer",
            ]
        )
        return [SearchResult(id="global_context", score=1.0, text=context)]
    raise AssertionError(mode)


def test_load_eval_cases_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text(
        json.dumps(
            [
                {"id": "same", "question": "one", "expected_entities": ["A"]},
                {"id": "same", "question": "two", "expected_entities": ["B"]},
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate evaluation case id"):
        load_eval_cases(path)


def test_repository_eval_set_is_valid_and_curated():
    path = Path(__file__).parents[1] / "eval" / "queries.json"
    cases = load_eval_cases(path)

    assert len(cases) == 12
    assert all(case.expected_source_documents for case in cases)
    assert sum(case.category == "global_theme" for case in cases) == 2


def test_term_matching_normalizes_punctuation_and_case():
    assert contains_term("The GRAPH_RAG processor is slow", "graph-rag")
    assert contains_term("Issue #944 discusses Kafka", "issue 944")


def test_rank_metrics_handle_multiple_relevant_documents():
    expected = ["doc-a", "doc-b"]
    retrieved = ["noise", "doc-b", "doc-a"]

    assert recall_at_k(expected, retrieved, top_k=2) == 0.5
    assert recall_at_k(expected, retrieved, top_k=3) == 1.0
    assert reciprocal_rank(expected, retrieved) == 0.5
    assert recall_at_k([], retrieved, top_k=3) is None


@pytest.mark.parametrize(
    ("mode", "expected_sources", "expected_communities"),
    [
        ("naive", ["repo#issue-875", "repo#issue-944"], []),
        ("local", ["repo#issue-944"], ["community-kafka"]),
        (
            "global",
            ["repo#issue-875", "repo#issue-944"],
            ["community-retrieval", "community-kafka"],
        ),
    ],
)
def test_trace_retrieval_extracts_ranked_evidence(
    retrieval_data,
    mode,
    expected_sources,
    expected_communities,
):
    graph, units, reports = retrieval_data

    trace = trace_retrieval(
        mode,
        "question",
        graph,
        units,
        reports,
        top_k=8,
        repeats=2,
        retrieve=_fake_retrieve,
    )

    assert trace.source_document_ids == expected_sources
    assert trace.community_ids == expected_communities
    assert trace.latency_ms >= 0


def test_evaluate_case_reports_entity_source_and_community_metrics(retrieval_data):
    graph, units, reports = retrieval_data
    case = EvalCase(
        id="kafka",
        category="global_theme",
        question="What affects Kafka reliability?",
        expected_entities=["Kafka", "Consumer", "Missing Entity"],
        expected_source_documents=["repo#issue-944"],
        expected_community_entities=["Kafka", "Consumer"],
    )

    row = evaluate_case(
        case,
        "global",
        graph,
        units,
        reports,
        top_k=8,
        retrieve=_fake_retrieve,
    )

    assert row.entity_recall == pytest.approx(2 / 3)
    assert row.source_recall_at_k == 1.0
    assert row.source_mrr is None
    assert row.community_recall == 1.0
    assert row.community_mrr == 0.5
    assert row.missing_entities == ["Missing Entity"]


def test_category_routing_keeps_global_out_of_specific_queries():
    case = EvalCase(
        id="specific",
        category="single_lookup",
        question="Specific question",
        expected_entities=["Entity"],
    )

    assert modes_for_case(case, ["naive", "local", "global"], False) == [
        "naive",
        "local",
    ]
    assert modes_for_case(case, ["naive", "local", "global"], True) == [
        "naive",
        "local",
        "global",
    ]


def test_summary_averages_available_metrics_only(retrieval_data):
    graph, units, reports = retrieval_data
    local_case = EvalCase(
        id="local",
        category="single_lookup",
        question="Kafka",
        expected_entities=["Kafka"],
        expected_source_documents=["repo#issue-944"],
    )
    row = evaluate_case(
        local_case,
        "local",
        graph,
        units,
        reports,
        top_k=8,
        retrieve=_fake_retrieve,
    )

    result = summarize([row])[0]
    assert result["entity_recall"] == 1.0
    assert result["source_recall_at_k"] == 1.0
    assert result["community_recall"] is None
