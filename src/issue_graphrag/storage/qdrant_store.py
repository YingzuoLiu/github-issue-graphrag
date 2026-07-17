from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient, models

from issue_graphrag.storage.vector_store import VectorMatch, VectorRecord


POINT_ID_NAMESPACE = uuid.NAMESPACE_URL


def stable_point_id(kind: str, source_id: str) -> str:
    """Return the UUID accepted by Qdrant for one stable logical identity."""

    if not kind or not source_id:
        raise ValueError("kind and source_id must be non-empty")
    return str(uuid.uuid5(POINT_ID_NAMESPACE, f"{kind}:{source_id}"))


class QdrantVectorStore:
    """Persistent Qdrant local-mode store; no server or Docker is required."""

    def __init__(
        self,
        path: Path,
        collection_name: str,
        vector_size: int,
    ):
        if vector_size < 1:
            raise ValueError("vector_size must be at least 1")
        if not collection_name:
            raise ValueError("collection_name must be non-empty")

        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.collection_name = collection_name
        self.vector_size = vector_size
        self.client = QdrantClient(path=str(path))
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.vector_size,
                    distance=models.Distance.COSINE,
                ),
            )
            return

        collection = self.client.get_collection(self.collection_name)
        vectors_config = collection.config.params.vectors
        existing_size = getattr(vectors_config, "size", None)
        if existing_size is not None and int(existing_size) != self.vector_size:
            self.close()
            raise ValueError(
                f"Collection {self.collection_name!r} has vector size {existing_size}; "
                f"expected {self.vector_size}"
            )

    @staticmethod
    def _kind_filter(kind: str | None) -> models.Filter | None:
        if kind is None:
            return None
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="kind",
                    match=models.MatchValue(value=kind),
                )
            ]
        )

    def upsert(self, records: list[VectorRecord]) -> None:
        if not records:
            return

        points: list[models.PointStruct] = []
        for record in records:
            if len(record.vector) != self.vector_size:
                raise ValueError(
                    f"Vector for {record.kind}:{record.source_id} has size "
                    f"{len(record.vector)}; expected {self.vector_size}"
                )
            points.append(
                models.PointStruct(
                    id=stable_point_id(record.kind, record.source_id),
                    vector=record.vector,
                    payload={
                        "kind": record.kind,
                        "source_id": record.source_id,
                        "text": record.text,
                        "metadata": record.metadata,
                    },
                )
            )

        self.client.upsert(
            collection_name=self.collection_name,
            points=points,
            wait=True,
        )

    def query(
        self,
        vector: list[float],
        limit: int,
        kind: str | None = None,
    ) -> list[VectorMatch]:
        if len(vector) != self.vector_size:
            raise ValueError(
                f"Query vector has size {len(vector)}; expected {self.vector_size}"
            )
        if limit < 1:
            raise ValueError("limit must be at least 1")

        response = self.client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=self._kind_filter(kind),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        matches: list[VectorMatch] = []
        for point in response.points:
            payload: dict[str, Any] = dict(point.payload or {})
            metadata = payload.get("metadata")
            matches.append(
                VectorMatch(
                    kind=str(payload.get("kind", "")),
                    source_id=str(payload.get("source_id", "")),
                    text=str(payload.get("text", "")),
                    score=float(point.score),
                    metadata=dict(metadata) if isinstance(metadata, dict) else {},
                )
            )
        return matches

    def delete(self, identities: list[tuple[str, str]]) -> None:
        if not identities:
            return
        point_ids: list[int | str | uuid.UUID] = [
            stable_point_id(kind, source_id) for kind, source_id in identities
        ]
        self.client.delete(
            collection_name=self.collection_name,
            points_selector=models.PointIdsList(points=point_ids),
            wait=True,
        )

    def count(self, kind: str | None = None) -> int:
        result = self.client.count(
            collection_name=self.collection_name,
            count_filter=self._kind_filter(kind),
            exact=True,
        )
        return int(result.count)

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> QdrantVectorStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        self.close()
