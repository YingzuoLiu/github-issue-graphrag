from __future__ import annotations

import hashlib

from issue_graphrag.indexing.extractor import EXTRACTION_RESPONSE_SCHEMA
from issue_graphrag.llm.client import OpenAICompatibleClient
from issue_graphrag.prompts import (
    ENTITY_EXTRACTION_PROMPT,
    ENTITY_EXTRACTION_PROMPT_SHA256,
    EXTRACTION_PROMPT_VERSION,
    assert_extraction_prompt_identity,
)


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "id": "gen-123",
            "model": "google/gemini-3.1-flash-lite-202608",
            "provider": "Google AI Studio",
            "choices": [{"message": {"content": '{"entities":[],"relationships":[]}'}}],
            "usage": {"prompt_tokens": 41, "completion_tokens": 9, "cost": 0.000024},
        }


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):  # noqa: ANN001
        self.calls.append((url, kwargs))
        return FakeResponse()


def test_extraction_prompt_version_and_exact_bytes_are_independently_pinned():
    assert EXTRACTION_PROMPT_VERSION == "extraction/2026-08-24"
    assert hashlib.sha256(ENTITY_EXTRACTION_PROMPT.encode()).hexdigest() == (
        ENTITY_EXTRACTION_PROMPT_SHA256
    )
    assert_extraction_prompt_identity()


def test_openrouter_structured_request_is_exact_and_usage_is_audit_metadata():
    session = FakeSession()
    client = OpenAICompatibleClient(
        "https://openrouter.ai/api/v1",
        "secret",
        "google/gemini-3.1-flash-lite",
        max_retries=1,
        session=session,
    )

    result = client.complete_structured(
        "extract this",
        schema_name="github_issue_graph_extraction",
        schema=EXTRACTION_RESPONSE_SCHEMA,
        max_tokens=800,
    )

    assert len(session.calls) == 1
    url, request = session.calls[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert request["json"] == {
        "model": "google/gemini-3.1-flash-lite",
        "messages": [{"role": "user", "content": "extract this"}],
        "temperature": 0,
        "max_tokens": 800,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "github_issue_graph_extraction",
                "strict": True,
                "schema": EXTRACTION_RESPONSE_SCHEMA,
            },
        },
        "provider": {"require_parameters": True},
    }
    assert "models" not in request["json"]
    assert result.metadata.requested_model == "google/gemini-3.1-flash-lite"
    assert result.metadata.actual_model == "google/gemini-3.1-flash-lite-202608"
    assert result.metadata.provider == "Google AI Studio"
    assert result.metadata.generation_id == "gen-123"
    assert result.metadata.input_tokens == 41
    assert result.metadata.output_tokens == 9
    assert result.metadata.cost_usd == 0.000024
