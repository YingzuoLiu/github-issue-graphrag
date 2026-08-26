from __future__ import annotations

import sys

import pytest

from scripts.operations_readiness import main
from issue_graphrag.live.repositories import repo_directory_name

REPO = "owner/repo"


def _viewer_environment(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    repo_data = tmp_path / "repos"
    repo_data.mkdir()
    monkeypatch.setenv("REPO_DATA_DIR", str(repo_data))
    monkeypatch.setenv("GITHUB_REPOS", REPO)
    monkeypatch.setenv("PUBLIC_RADAR_ONLY", "1")
    monkeypatch.setenv(
        "RADAR_ANALYTICS_PATH",
        str(tmp_path / "missing-analytics" / "radar.sqlite"),
    )
    for name in ("GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"{name}_FILE", raising=False)
    monkeypatch.setattr(sys, "argv", ["operations_readiness.py", "viewer"])


def test_analytics_failure_is_degraded_but_does_not_block_viewer_readiness(
    monkeypatch,
    tmp_path,
    capsys,
):
    _viewer_environment(monkeypatch, tmp_path)

    main()

    output = capsys.readouterr()
    assert "viewer ready" in output.out
    assert "analytics degraded" in output.err


def test_incomplete_restore_blocks_viewer_readiness(monkeypatch, tmp_path):
    _viewer_environment(monkeypatch, tmp_path)
    audit = (
        tmp_path
        / "repos"
        / ".operations"
        / "restores"
        / repo_directory_name(REPO)
        / "pending.json"
    )
    audit.parent.mkdir(parents=True)
    audit.write_text('{"status":"planned"}\n', encoding="utf-8")

    with pytest.raises(SystemExit, match="restore is incomplete"):
        main()
