from __future__ import annotations

import math
import re
from collections import Counter

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

        for part in re.split(r"[_\-.]+", token):
            if part and part not in _STOPWORDS:
                tokens.append(part)

    return tokens


def naive_search(text_units: list[TextUnit], query: str, top_k: int = 8) -> list[SearchResult]:
    """Simple lexical baseline search over TextUnits.

    This is intentionally small: it gives us a baseline to compare against
    local/global GraphRAG retrieval.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return []

    documents = []
    document_freq = Counter()

    for unit in text_units:
        text = f"{unit.metadata.get('document_title', '')}\n{unit.text}"
        tokens = _tokens(text)
        token_counts = Counter(tokens)
        documents.append((unit, text, token_counts))

        for token in set(tokens):
            document_freq[token] += 1

    total_docs = max(len(documents), 1)
    query_counts = Counter(query_tokens)

    scored: list[tuple[float, TextUnit, str]] = []

    for unit, text, token_counts in documents:
        score = 0.0

        for token, q_count in query_counts.items():
            tf = token_counts.get(token, 0)
            if tf == 0:
                continue

            idf = math.log((total_docs + 1) / (document_freq[token] + 1)) + 1
            score += q_count * tf * idf

        if score > 0:
            scored.append((score, unit, text))

    scored.sort(key=lambda item: item[0], reverse=True)

    results: list[SearchResult] = []
    for score, unit, text in scored[:top_k]:
        title = unit.metadata.get("document_title", "")
        results.append(
            SearchResult(
                id=unit.id,
                score=float(score),
                text=f"[{unit.id}] {title}\n{text[:1600]}",
            )
        )

    return results
