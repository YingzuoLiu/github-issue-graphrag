"""Shared construction of live extractors for replay and worker CLIs."""

from __future__ import annotations

from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.extraction import Extractor, FixtureExtractor, LLMExtractor
from issue_graphrag.live.indexer import NullExtractor
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient


def configured_llm():
    settings = load_settings()
    if settings.llm_provider == "openai-compatible":
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required")
        return OpenAICompatibleClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
        )
    return MockLLMClient()


def configured_extractor(
    *,
    rules: Path | None = None,
    use_llm: bool = False,
) -> Extractor:
    if rules is not None and use_llm:
        raise ValueError("choose fixture rules or a live LLM, not both")
    if rules is not None:
        return FixtureExtractor.from_path(rules)
    if use_llm:
        return LLMExtractor(configured_llm())
    return NullExtractor()
