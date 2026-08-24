"""Shared construction of live extractors for replay and worker CLIs."""

from __future__ import annotations

from pathlib import Path

from issue_graphrag.config import Settings, load_settings
from issue_graphrag.live.extraction import Extractor, FixtureExtractor, LLMExtractor
from issue_graphrag.live.indexer import NullExtractor
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def validate_openrouter_operations(settings: Settings) -> None:
    """Fail closed if live extraction could escape the reviewed gateway policy."""
    if settings.llm_provider != "openrouter":
        raise ValueError("live LLM extraction requires LLM_PROVIDER=openrouter")
    if (settings.llm_base_url or "").rstrip("/") != OPENROUTER_BASE_URL:
        raise ValueError(f"live OpenRouter base URL must be {OPENROUTER_BASE_URL}")
    model = settings.llm_model or ""
    if not model:
        raise ValueError("live OpenRouter extraction requires an exact LLM_MODEL")
    if model in {"openrouter/auto", "openrouter/free"} or model.endswith(":free"):
        raise ValueError("live extraction forbids free/auto router models")


def configured_llm(*, operational: bool = False):
    settings = load_settings()
    if settings.llm_provider in {"openrouter", "openai-compatible"}:
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required")
        return OpenAICompatibleClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            max_retries=1 if operational else 5,
        )
    return MockLLMClient()


def configured_extractor(
    *,
    rules: Path | None = None,
    use_llm: bool = False,
    operational: bool = False,
) -> Extractor:
    if rules is not None and use_llm:
        raise ValueError("choose fixture rules or a live LLM, not both")
    if rules is not None:
        return FixtureExtractor.from_path(rules)
    if use_llm:
        return LLMExtractor(configured_llm(operational=operational))
    return NullExtractor()
