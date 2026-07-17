from __future__ import annotations

from typing import Protocol


class EmbeddingClient(Protocol):
    """Small embedding interface kept independent from any model vendor."""

    @property
    def dimension(self) -> int:
        """Return the fixed vector dimension produced by this client."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of indexable documents."""

    def embed_query(self, text: str) -> list[float]:
        """Embed one retrieval query."""
