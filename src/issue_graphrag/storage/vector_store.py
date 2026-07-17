from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class VectorRecord:
    kind: str
    source_id: str
    text: str
    vector: list[float]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorMatch:
    kind: str
    source_id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore(Protocol):
    def upsert(self, records: list[VectorRecord]) -> None:
        """Insert or replace records by their stable identity."""

    def query(
        self,
        vector: list[float],
        limit: int,
        kind: str | None = None,
    ) -> list[VectorMatch]:
        """Return nearest records, optionally restricted to one record kind."""

    def delete(self, identities: list[tuple[str, str]]) -> None:
        """Delete records identified by (kind, source_id)."""

    def count(self, kind: str | None = None) -> int:
        """Count all records or records of one kind."""

    def close(self) -> None:
        """Release storage resources and local file locks."""
