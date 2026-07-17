from __future__ import annotations

import argparse
import time

from issue_graphrag.config import load_settings
from issue_graphrag.embeddings.sentence_transformer import (
    SentenceTransformerEmbeddingClient,
)
from issue_graphrag.indexing.vector_documents import VectorDocument, build_vector_documents
from issue_graphrag.models import CommunityReport, Entity, TextUnit
from issue_graphrag.storage.json_store import read_json
from issue_graphrag.storage.qdrant_store import QdrantVectorStore
from issue_graphrag.storage.vector_store import VectorRecord


def _load_documents() -> list[VectorDocument]:
    settings = load_settings()
    processed = settings.processed_data_dir
    text_units = [
        TextUnit.model_validate(item)
        for item in read_json(processed / "text_units.json")
    ]
    entities = [
        Entity.model_validate(item)
        for item in read_json(processed / "entities.json")
    ]
    reports = [
        CommunityReport.model_validate(item)
        for item in read_json(processed / "community_reports.json")
    ]
    return build_vector_documents(text_units, entities, reports)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the optional embedded Qdrant index."
    )
    parser.add_argument("--batch-size", type=int, default=32)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    settings = load_settings()
    if settings.embedding_provider != "sentence-transformers":
        raise ValueError(
            "Set EMBEDDING_PROVIDER=sentence-transformers to build the vector index"
        )

    started = time.perf_counter()
    embedding = SentenceTransformerEmbeddingClient(
        settings.embedding_model,
        batch_size=args.batch_size,
    )
    model_load_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    documents = _load_documents()
    document_load_ms = (time.perf_counter() - started) * 1000
    counts: dict[str, int] = {}

    started = time.perf_counter()
    with QdrantVectorStore(
        path=settings.vector_db_path,
        collection_name=settings.vector_collection,
        vector_size=embedding.dimension,
    ) as store:
        for start in range(0, len(documents), args.batch_size):
            batch = documents[start : start + args.batch_size]
            vectors = embedding.embed_documents([document.text for document in batch])
            records = [
                VectorRecord(
                    kind=document.kind,
                    source_id=document.source_id,
                    text=document.text,
                    vector=vector,
                    metadata=document.metadata,
                )
                for document, vector in zip(batch, vectors, strict=True)
            ]
            store.upsert(records)

        for kind in ("text_unit", "entity", "community_report"):
            counts[kind] = store.count(kind)
    index_build_ms = (time.perf_counter() - started) * 1000

    print(f"Built embedded Qdrant index at {settings.vector_db_path}")
    print(f"Collection: {settings.vector_collection}")
    print(counts)
    print(
        {
            "embedding_model_load_ms": round(model_load_ms, 2),
            "document_load_ms": round(document_load_ms, 2),
            "embedding_and_upsert_ms": round(index_build_ms, 2),
        }
    )


if __name__ == "__main__":
    main()
