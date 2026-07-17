from __future__ import annotations

from issue_graphrag.embeddings.base import EmbeddingClient
from issue_graphrag.models import SearchResult
from issue_graphrag.storage.vector_store import VectorStore


class VectorRetriever:
    """Dense TextUnit retrieval backed by the configured vector store."""

    def __init__(self, embedding: EmbeddingClient, store: VectorStore):
        self.embedding = embedding
        self.store = store

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        if not query.strip():
            return []

        vector = self.embedding.embed_query(query)
        matches = self.store.query(vector, limit=top_k, kind="text_unit")
        return [
            SearchResult(
                id=match.source_id,
                score=match.score,
                text=f"[{match.source_id}] {match.text[:1600]}",
                metadata={**match.metadata, "retriever": "vector"},
            )
            for match in matches
        ]
