from __future__ import annotations

from typing import Any


class SentenceTransformerEmbeddingClient:
    """Local sentence-transformers provider with normalized output vectors."""

    def __init__(self, model_name: str, batch_size: int = 32):
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required; install the embeddings extra"
            ) from exc

        self.model_name = model_name
        self.batch_size = batch_size
        self._model: Any = SentenceTransformer(model_name)
        dimension = self._model.get_sentence_embedding_dimension()
        if not dimension:
            raise ValueError(f"Embedding model {model_name!r} did not report a dimension")
        self._dimension = int(dimension)

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            texts,
            batch_size=self.batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]
