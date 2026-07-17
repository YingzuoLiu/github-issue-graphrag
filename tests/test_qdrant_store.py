import uuid

import pytest

from issue_graphrag.storage.qdrant_store import QdrantVectorStore, stable_point_id
from issue_graphrag.storage.vector_store import VectorRecord


def _record(
    kind: str,
    source_id: str,
    vector: list[float],
    text: str = "text",
) -> VectorRecord:
    return VectorRecord(
        kind=kind,
        source_id=source_id,
        text=text,
        vector=vector,
        metadata={"version": text},
    )


def test_stable_point_id_is_deterministic_uuid():
    first = stable_point_id("text_unit", "source-1")
    second = stable_point_id("text_unit", "source-1")

    assert first == second
    assert uuid.UUID(first).version == 5
    assert first != stable_point_id("entity", "source-1")


def test_qdrant_local_upsert_is_idempotent_and_queryable(tmp_path):
    path = tmp_path / "qdrant"
    with QdrantVectorStore(path, "test", vector_size=3) as store:
        store.upsert([_record("text_unit", "source-1", [1.0, 0.0, 0.0], "old")])
        store.upsert([_record("text_unit", "source-1", [0.0, 1.0, 0.0], "new")])
        store.upsert([_record("entity", "source-1", [0.0, 0.0, 1.0], "entity")])

        assert store.count() == 2
        assert store.count("text_unit") == 1
        matches = store.query([0.0, 1.0, 0.0], limit=2, kind="text_unit")
        assert len(matches) == 1
        assert matches[0].kind == "text_unit"
        assert matches[0].source_id == "source-1"
        assert matches[0].text == "new"
        assert matches[0].metadata == {"version": "new"}

        store.delete([("text_unit", "source-1")])
        assert store.count() == 1
        assert store.count("text_unit") == 0


def test_qdrant_local_persists_after_reopen(tmp_path):
    path = tmp_path / "qdrant"
    with QdrantVectorStore(path, "test", vector_size=2) as store:
        store.upsert([_record("text_unit", "source-1", [1.0, 0.0])])

    with QdrantVectorStore(path, "test", vector_size=2) as reopened:
        assert reopened.count() == 1
        assert reopened.query([1.0, 0.0], limit=1)[0].source_id == "source-1"


def test_qdrant_local_rejects_dimension_mismatch(tmp_path):
    path = tmp_path / "qdrant"
    with QdrantVectorStore(path, "test", vector_size=2):
        pass

    with pytest.raises(ValueError, match="has vector size 2; expected 3"):
        QdrantVectorStore(path, "test", vector_size=3)


def test_qdrant_local_rejects_invalid_vectors(tmp_path):
    with QdrantVectorStore(tmp_path / "qdrant", "test", vector_size=2) as store:
        with pytest.raises(ValueError, match="expected 2"):
            store.upsert([_record("text_unit", "source-1", [1.0])])
        with pytest.raises(ValueError, match="expected 2"):
            store.query([1.0], limit=1)
