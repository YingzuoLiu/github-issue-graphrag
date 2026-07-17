from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

from issue_graphrag.models import SearchResult, TextUnit


_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "in", "on", "with", "and", "or", "by", "from",
    "how", "what", "why", "which", "who", "where", "when", "can", "could",
    "should", "would", "about",
}


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9][a-z0-9_\-.]*", (text or "").lower())
    tokens: list[str] = []

    for token in raw:
        if token not in _STOPWORDS:
            tokens.append(token)

        # Also index decomposed variants so identifiers such as
        # `kafka_backend.py`, `Graph-RAG`, and `document_rag.py` can match
        # natural-language queries like "kafka backend" or "graph rag".
        for part in re.split(r"[_\-.]+", token):
            if part and part not in _STOPWORDS:
                tokens.append(part)

    return tokens


class BM25Retriever:
    """Reusable BM25 index over TextUnits."""

    def __init__(self, text_units: list[TextUnit]):
        self._documents: list[tuple[TextUnit, str]] = []
        corpus: list[list[str]] = []

        for unit in text_units:
            title = unit.metadata.get("document_title", "")
            text = f"{title}\n{unit.text}"
            tokens = _tokens(text)
            if not tokens:
                continue

            self._documents.append((unit, text))
            corpus.append(tokens)

        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")

        query_tokens = _tokens(query)
        if not query_tokens or self._bm25 is None:
            return []

        scores = self._bm25.get_scores(query_tokens)
        ranked_indices = sorted(
            range(len(scores)),
            key=lambda index: (-float(scores[index]), index),
        )

        results: list[SearchResult] = []
        for index in ranked_indices[:top_k]:
            score = float(scores[index])
            if score <= 0:
                continue

            unit, text = self._documents[index]
            title = unit.metadata.get("document_title", "")
            results.append(
                SearchResult(
                    id=unit.id,
                    score=score,
                    text=f"[{unit.id}] {title}\n{text[:1600]}",
                    metadata={
                        "document_id": unit.document_id,
                        "retriever": "bm25",
                    },
                )
            )

        return results


def naive_search(text_units: list[TextUnit], query: str, top_k: int = 8) -> list[SearchResult]:
    """Compatibility wrapper for the BM25 lexical baseline.

    This baseline intentionally ignores the graph and ranks source TextUnits
    directly with BM25. It gives us a stronger lexical comparison point for
    local/global GraphRAG retrieval. Callers that run multiple queries should
    construct :class:`BM25Retriever` once and reuse its index.
    """
    return BM25Retriever(text_units).search(query, top_k=top_k)
