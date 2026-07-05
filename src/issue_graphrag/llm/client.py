from __future__ import annotations

import json
from typing import Protocol


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
    """Minimal OpenAI-compatible chat client.

    This avoids locking the project to one model provider. It can be used with
    OpenAI-compatible endpoints from different vendors.
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    def complete(self, prompt: str) -> str:
        import requests

        url = f"{self.base_url}/chat/completions"
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
