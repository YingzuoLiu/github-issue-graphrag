from __future__ import annotations

import pytest

from issue_graphrag.config import load_settings
from issue_graphrag.live.operations import validate_public_viewer
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


def test_secrets_can_come_from_files_but_never_both_sources(monkeypatch, tmp_path):
    secret_file = tmp_path / "github-token"
    secret_file.write_text("token-from-file\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(secret_file))

    assert load_settings().github_token == "token-from-file"

    monkeypatch.setenv("GITHUB_TOKEN", "token-from-env")
    with pytest.raises(ValueError, match="set only one"):
        load_settings()

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(tmp_path / "missing-token"))
    with pytest.raises(ValueError, match="readable regular file"):
        load_settings()


def test_public_viewer_rejects_credentials_and_has_separate_analytics(monkeypatch, tmp_path):
    analytics = tmp_path / "analytics" / "radar.sqlite"
    monkeypatch.setenv("PUBLIC_RADAR_ONLY", "true")
    monkeypatch.setenv("RADAR_ANALYTICS_PATH", str(analytics))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN_FILE", raising=False)
    settings = load_settings()

    validate_public_viewer(settings)
    assert settings.public_radar_only is True
    assert settings.radar_analytics_path == analytics

    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-viewer")
    with pytest.raises(ValueError, match="public Viewer must not receive credentials"):
        validate_public_viewer(load_settings())


def test_public_viewer_rejects_analytics_inside_repository_data(monkeypatch, tmp_path):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("PUBLIC_RADAR_ONLY", "1")
    monkeypatch.setenv("REPO_DATA_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv(
        "RADAR_ANALYTICS_PATH",
        str(tmp_path / "repos" / "radar_analytics.sqlite"),
    )

    with pytest.raises(ValueError, match="analytics must be outside"):
        validate_public_viewer(load_settings())


def test_invalid_boolean_configuration_is_rejected(monkeypatch):
    monkeypatch.setenv("PUBLIC_RADAR_ONLY", "sometimes")
    with pytest.raises(ValueError, match="must be true or false"):
        load_settings()
