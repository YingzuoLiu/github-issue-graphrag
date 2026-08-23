from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

import requests


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str:
        """Return a text completion for a prompt."""


@dataclass(frozen=True)
class CompletionMetadata:
    requested_model: str
    actual_model: str | None
    provider: str | None
    generation_id: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def __post_init__(self) -> None:
        if not self.requested_model:
            raise ValueError("requested model metadata cannot be empty")
        if self.input_tokens < 0 or self.output_tokens < 0 or self.cost_usd < 0:
            raise ValueError("completion usage metadata cannot be negative")


@dataclass(frozen=True)
class StructuredCompletion:
    content: str
    metadata: CompletionMetadata


class MockLLMClient:
    """Deterministic mock used to test the pipeline before connecting a real LLM."""

    def complete(self, prompt: str) -> str:
        lowered = prompt.lower()
        if "strict json" in lowered and "entities" in lowered and "relationships" in lowered:
            return json.dumps({"entities": [], "relationships": []})
        if "community report" in lowered or "community data" in lowered:
            return json.dumps(
                {
                    "title": "Untitled community",
                    "summary": "Mock community report. Connect a real LLM for grounded summaries.",
                    "rating": 1.0,
                }
            )
        if "points" in lowered and "score" in lowered:
            return json.dumps({"points": []})
        return "Mock answer. Connect a real LLM provider for generated answers."


class OpenAICompatibleClient:
    """Minimal OpenAI-compatible chat client with retry support."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        max_retries: int = 5,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()

    def complete(self, prompt: str) -> str:
        return self._request(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            }
        ).content

    def complete_structured(
        self,
        prompt: str,
        *,
        schema_name: str,
        schema: dict,
        max_tokens: int,
        require_parameters: bool = True,
    ) -> StructuredCompletion:
        """Return one auditable strict-schema completion.

        The requested model is exact. OpenRouter may select a different
        provider endpoint for that same model, but no model list, free router,
        or auto router is sent.
        """
        return self._request(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": schema,
                    },
                },
                "provider": {"require_parameters": require_parameters},
            }
        )

    def _request(self, payload: dict) -> StructuredCompletion:
        url = f"{self.base_url}/chat/completions"

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Connection": "close",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage") or {}
                return StructuredCompletion(
                    content=data["choices"][0]["message"]["content"],
                    metadata=CompletionMetadata(
                        requested_model=self.model,
                        actual_model=data.get("model"),
                        provider=data.get("provider"),
                        generation_id=data.get("id"),
                        input_tokens=int(usage.get("prompt_tokens") or 0),
                        output_tokens=int(usage.get("completion_tokens") or 0),
                        cost_usd=float(usage.get("cost") or 0.0),
                    ),
                )

            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                wait_seconds = min(2 ** attempt, 30)
                print(
                    f"[LLM retry] attempt {attempt}/{self.max_retries} failed: {exc}. "
                    f"Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)

        raise RuntimeError(f"LLM request failed after {self.max_retries} retries") from last_error
