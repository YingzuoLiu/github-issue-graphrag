from __future__ import annotations

import pytest

from issue_graphrag.config import load_settings
from issue_graphrag.live.runtime import configured_llm, validate_openrouter_operations


def test_repository_configuration_combines_selector_and_webhook_lane(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOS", "Alpha/One, beta/two,Alpha/One")
    monkeypatch.setenv("GITHUB_WEBHOOK_REPO", "gamma/three")
    monkeypatch.setenv("REPO_DATA_DIR", str(tmp_path / "repos"))

    settings = load_settings()

    assert settings.github_repos == ("Alpha/One", "beta/two", "gamma/three")
    assert settings.github_webhook_repo == "gamma/three"
    assert settings.repo_data_dir == tmp_path / "repos"


def test_scheduled_sync_interval_defaults_and_is_operator_configurable(monkeypatch):
    monkeypatch.delenv("GITHUB_SYNC_INTERVAL_SECONDS", raising=False)
    assert load_settings().github_sync_interval_seconds == 900

    monkeypatch.setenv("GITHUB_SYNC_INTERVAL_SECONDS", "1200")
    assert load_settings().github_sync_interval_seconds == 1200


def test_m4_openrouter_defaults_and_limits_are_operator_configurable(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_DAILY_CALLS", "17")
    monkeypatch.setenv("LLM_MONTHLY_COST_USD", "0")

    settings = load_settings()

    assert settings.llm_base_url == "https://openrouter.ai/api/v1"
    assert settings.llm_model == "google/gemini-3.1-flash-lite"
    assert settings.llm_daily_calls == 17
    assert settings.llm_monthly_cost_usd == 0
    assert settings.llm_batch_calls == 12
    assert settings.llm_max_output_tokens_per_call == 800
    validate_openrouter_operations(settings)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    assert configured_llm(operational=True).max_retries == 1


def test_live_operations_reject_free_or_auto_router_models(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL", "openrouter/auto")

    with pytest.raises(ValueError, match="forbids free/auto"):
        validate_openrouter_operations(load_settings())
