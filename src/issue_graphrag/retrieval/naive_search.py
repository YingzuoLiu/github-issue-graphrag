from __future__ import annotations

from rank_bm25 import BM25Okapi

from issue_graphrag.models import SearchResult, TextUnit


def _tokenize(text: str) -> list[str]:
    return text.lower().replace("/", " ").replace("_", " ").split()


class NaiveBM25Search:
    """Simple BM25 chunk search baseline."""

    def __init__(self, text_units: list[TextUnit]):
        self.text_units = text_units
        self.corpus = [_tokenize(t.text) for t in text_units]
        self.index = BM25Okapi(self.corpus) if self.corpus else None

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        if not self.index:
            return []
        scores = self.index.get_scores(_tokenize(query))
        ranked = sorted(enumerate(scores), key=lambda item: float(item[1]), reverse=True)[:top_k]
        return [
            SearchResult(
                id=self.text_units[idx].id,
                score=float(score),
                text=self.text_units[idx].text,
                metadata=self.text_units[idx].metadata,
            )
            for idx, score in ranked
            if score > 0
        ]
