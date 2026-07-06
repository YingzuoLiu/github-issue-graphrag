from __future__ import annotations

import json
import time
from typing import Protocol

import requests


class LLMClient(Protocol):
    def complete(self, prompt: str) -> str:
        """Return a text completion for a prompt."""


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
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries

    def complete(self, prompt: str) -> str:
        url = f"{self.base_url}/chat/completions"

        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "Connection": "close",
                    },
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                    },
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]

            except requests.RequestException as exc:
                last_error = exc
                wait_seconds = min(2 ** attempt, 30)
                print(
                    f"[LLM retry] attempt {attempt}/{self.max_retries} failed: {exc}. "
                    f"Retrying in {wait_seconds}s..."
                )
                time.sleep(wait_seconds)

        raise RuntimeError(f"LLM request failed after {self.max_retries} retries") from last_error
