import sys
import types

import pytest

from issue_graphrag.embeddings.sentence_transformer import (
    SentenceTransformerEmbeddingClient,
)


class FakeSentenceTransformer:
    def __init__(self, model_name):
        self.model_name = model_name

    def get_sentence_embedding_dimension(self):
        return 2

    def encode(self, texts, **kwargs):
        assert kwargs["normalize_embeddings"] is True
        assert kwargs["convert_to_numpy"] is True
        return [[0.6, 0.8] for _ in texts]


def test_sentence_transformer_client_batches_normalized_vectors(monkeypatch):
    module = types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer)
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    client = SentenceTransformerEmbeddingClient("test-model", batch_size=4)

    assert client.dimension == 2
    assert client.embed_documents(["one", "two"]) == [[0.6, 0.8], [0.6, 0.8]]
    assert client.embed_query("query") == [0.6, 0.8]
    assert client.embed_documents([]) == []


def test_sentence_transformer_client_has_actionable_optional_dependency_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)

    with pytest.raises(RuntimeError, match="install the embeddings extra"):
        SentenceTransformerEmbeddingClient("missing")
