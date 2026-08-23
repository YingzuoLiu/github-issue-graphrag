from __future__ import annotations

from issue_graphrag.config import load_settings


def test_repository_configuration_combines_selector_and_webhook_lane(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_REPOS", "Alpha/One, beta/two,Alpha/One")
    monkeypatch.setenv("GITHUB_WEBHOOK_REPO", "gamma/three")
    monkeypatch.setenv("REPO_DATA_DIR", str(tmp_path / "repos"))

    settings = load_settings()

    assert settings.github_repos == ("Alpha/One", "beta/two", "gamma/three")
    assert settings.github_webhook_repo == "gamma/three"
    assert settings.repo_data_dir == tmp_path / "repos"
