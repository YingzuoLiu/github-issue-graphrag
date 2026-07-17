from __future__ import annotations

from typing import Protocol

from issue_graphrag.models import SearchResult


DEFAULT_RRF_K = 60
DEFAULT_FUSION_DEPTH = 20


class RankedRetriever(Protocol):
    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        """Return results ordered from most to least relevant."""


def reciprocal_rank_fusion(
    rankings: dict[str, list[SearchResult]],
    top_k: int,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[SearchResult]:
    """Fuse rankings by identifier using RRF scores.

    ``rrf_k=60`` is the conventional default used for this experiment. It has
    not been tuned on the repository evaluation set.
    """

    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if rrf_k < 1:
        raise ValueError("rrf_k must be at least 1")

    scores: dict[str, float] = {}
    ranks: dict[str, dict[str, int]] = {}
    originals: dict[str, SearchResult] = {}

    for name, results in rankings.items():
        seen: set[str] = set()
        for rank, result in enumerate(results, start=1):
            if result.id in seen:
                continue
            seen.add(result.id)
            scores[result.id] = scores.get(result.id, 0.0) + 1.0 / (rrf_k + rank)
            ranks.setdefault(result.id, {})[name] = rank
            originals.setdefault(result.id, result)

    ranked_ids = sorted(
        scores,
        key=lambda result_id: (
            -scores[result_id],
            min(ranks[result_id].values()),
            result_id,
        ),
    )

    fused: list[SearchResult] = []
    for result_id in ranked_ids[:top_k]:
        original = originals[result_id]
        fused.append(
            SearchResult(
                id=result_id,
                score=scores[result_id],
                text=original.text,
                metadata={
                    **original.metadata,
                    "retriever": "rrf_hybrid",
                    "rrf_k": rrf_k,
                    "rrf_ranks": ranks[result_id],
                },
            )
        )
    return fused


class HybridRetriever:
    """Fuse lexical and dense rankings without mixing raw score scales."""

    def __init__(
        self,
        lexical: RankedRetriever,
        vector: RankedRetriever,
        fusion_depth: int = DEFAULT_FUSION_DEPTH,
        rrf_k: int = DEFAULT_RRF_K,
    ):
        if fusion_depth < 1:
            raise ValueError("fusion_depth must be at least 1")
        if rrf_k < 1:
            raise ValueError("rrf_k must be at least 1")
        self.lexical = lexical
        self.vector = vector
        self.fusion_depth = fusion_depth
        self.rrf_k = rrf_k

    def search(self, query: str, top_k: int = 8) -> list[SearchResult]:
        if top_k < 1:
            raise ValueError("top_k must be at least 1")
        candidate_k = max(top_k, self.fusion_depth)
        return reciprocal_rank_fusion(
            {
                "bm25": self.lexical.search(query, top_k=candidate_k),
                "vector": self.vector.search(query, top_k=candidate_k),
            },
            top_k=top_k,
            rrf_k=self.rrf_k,
        )
