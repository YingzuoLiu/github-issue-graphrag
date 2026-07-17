from __future__ import annotations

import pytest

from issue_graphrag.models import SearchResult, TextUnit
from issue_graphrag.retrieval.hybrid_search import (
    DEFAULT_FUSION_DEPTH,
    DEFAULT_RRF_K,
    HybridRetriever,
    reciprocal_rank_fusion,
)
from issue_graphrag.retrieval.naive_search import BM25Retriever, naive_search
from issue_graphrag.retrieval.vector_search import VectorRetriever
from issue_graphrag.storage.vector_store import VectorMatch


def _unit(unit_id: str, document_id: str, text: str) -> TextUnit:
    return TextUnit(
        id=unit_id,
        document_id=document_id,
        text=text,
        order=0,
        metadata={"document_title": document_id},
    )


def test_bm25_retriever_reuses_index_and_preserves_grounding():
    units = [
        _unit("kafka", "issue-944", "Kafka consumer unsubscribe blocking"),
        _unit("vector", "issue-875", "dense semantic embeddings"),
        _unit("graph", "issue-922", "graph traversal latency"),
    ]
    retriever = BM25Retriever(units)

    first = retriever.search("Kafka unsubscribe", top_k=2)
    second = retriever.search("Kafka unsubscribe", top_k=2)

    assert [result.id for result in first] == ["kafka"]
    assert second == first
    assert first[0].metadata["document_id"] == "issue-944"
    assert naive_search(units, "Kafka unsubscribe", top_k=2)[0].id == "kafka"


class FakeEmbedding:
    dimension = 2

    def __init__(self):
        self.queries: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.0]


class FakeStore:
    def __init__(self):
        self.calls: list[tuple[list[float], int, str | None]] = []

    def query(self, vector, limit, kind=None):
        self.calls.append((vector, limit, kind))
        return [
            VectorMatch(
                kind="text_unit",
                source_id="unit-vector",
                text="Issue #875\nHybrid retrieval with dense vectors",
                score=0.9,
                metadata={"document_id": "repo#issue-875"},
            )
        ]


def test_vector_retriever_queries_only_text_units():
    embedding = FakeEmbedding()
    store = FakeStore()
    retriever = VectorRetriever(embedding, store)

    results = retriever.search("hybrid retrieval", top_k=5)

    assert embedding.queries == ["hybrid retrieval"]
    assert store.calls == [([1.0, 0.0], 5, "text_unit")]
    assert results[0].id == "unit-vector"
    assert results[0].metadata["document_id"] == "repo#issue-875"
    assert results[0].metadata["retriever"] == "vector"


def _result(result_id: str, score: float = 1.0) -> SearchResult:
    return SearchResult(id=result_id, score=score, text=f"[{result_id}] evidence")


def test_rrf_fuses_by_rank_with_explicit_default():
    fused = reciprocal_rank_fusion(
        {
            "bm25": [_result("a"), _result("b")],
            "vector": [_result("b"), _result("c")],
        },
        top_k=3,
    )

    assert DEFAULT_RRF_K == 60
    assert [result.id for result in fused] == ["b", "a", "c"]
    assert fused[0].score == pytest.approx(1 / 62 + 1 / 61)
    assert fused[0].metadata["rrf_ranks"] == {"bm25": 2, "vector": 1}


class RecordingRetriever:
    def __init__(self, results: list[SearchResult]):
        self.results = results
        self.top_ks: list[int] = []

    def search(self, _query: str, top_k: int = 8) -> list[SearchResult]:
        self.top_ks.append(top_k)
        return self.results[:top_k]


def test_hybrid_retriever_uses_named_candidate_depth():
    lexical = RecordingRetriever([_result("lexical")])
    vector = RecordingRetriever([_result("vector")])
    hybrid = HybridRetriever(lexical, vector)

    results = hybrid.search("query", top_k=1)

    assert DEFAULT_FUSION_DEPTH == 20
    assert lexical.top_ks == [20]
    assert vector.top_ks == [20]
    assert len(results) == 1
