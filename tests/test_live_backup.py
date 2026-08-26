from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_graphrag.live.backup import (
    BackupError,
    create_backup,
    pending_restore_count,
    restore_backup,
)
from issue_graphrag.live.repositories import RepoRegistry

REPO = "owner/repo"
NOW = "2026-08-24T02:00:00Z"


def _source(tmp_path):  # noqa: ANN001
    repo_data = tmp_path / "repos"
    paths = RepoRegistry(repo_data).register(REPO)
    paths.state.write_text('{"generation":"before"}\n', encoding="utf-8")
    paths.event_log.write_text("event-before\n", encoding="utf-8")
    analytics = tmp_path / "analytics" / "radar.sqlite"
    analytics.parent.mkdir(parents=True)
    analytics.write_bytes(b"analytics-before")
    return repo_data, paths, analytics


def test_backup_dry_run_is_read_only_and_actual_backup_requires_paused_services(tmp_path):
    repo_data, _, analytics = _source(tmp_path)
    output = tmp_path / "backup"

    plan = create_backup(
        repo_data_dir=repo_data,
        analytics_path=analytics,
        repo=REPO,
        output=output,
        dry_run=True,
        services_stopped=False,
        created_at=NOW,
    )
    assert plan.outcome == "dry_run" and plan.files == 3
    assert not output.exists()

    with pytest.raises(BackupError, match="confirm-services-stopped"):
        create_backup(
            repo_data_dir=repo_data,
            analytics_path=analytics,
            repo=REPO,
            output=output,
            dry_run=False,
            services_stopped=False,
            created_at=NOW,
        )


def test_validated_backup_restore_round_trip_and_quarantines_current_lane(tmp_path):
    repo_data, paths, analytics = _source(tmp_path)
    output = tmp_path / "backup"
    create_backup(
        repo_data_dir=repo_data,
        analytics_path=analytics,
        repo=REPO,
        output=output,
        dry_run=False,
        services_stopped=True,
        created_at=NOW,
    )
    paths.state.write_text('{"generation":"after"}\n', encoding="utf-8")
    analytics.write_bytes(b"analytics-after")
    Path(f"{analytics}-wal").write_bytes(b"stale-wal")
    Path(f"{analytics}-shm").write_bytes(b"stale-shm")

    dry_run = restore_backup(
        repo_data_dir=repo_data,
        analytics_path=analytics,
        repo=REPO,
        backup_path=output,
        dry_run=True,
        services_stopped=False,
        confirm_repo=None,
        restored_at="2026-08-24T03:00:00Z",
    )
    assert dry_run.outcome == "dry_run"
    assert "after" in paths.state.read_text(encoding="utf-8")

    restored = restore_backup(
        repo_data_dir=repo_data,
        analytics_path=analytics,
        repo=REPO,
        backup_path=output,
        dry_run=False,
        services_stopped=True,
        confirm_repo=REPO,
        restored_at="2026-08-24T03:00:00Z",
    )
    assert "before" in paths.state.read_text(encoding="utf-8")
    assert analytics.read_bytes() == b"analytics-before"
    assert not Path(f"{analytics}-wal").exists()
    assert not Path(f"{analytics}-shm").exists()
    assert restored.quarantine_path is not None
    assert restored.analytics_quarantine_path is not None
    assert "after" in (
        Path(restored.quarantine_path) / "repo" / "live_state.json"
    ).read_text(encoding="utf-8")
    analytics_quarantine = Path(restored.analytics_quarantine_path)
    assert (analytics_quarantine / "radar.sqlite").read_bytes() == b"analytics-after"
    assert (analytics_quarantine / "radar.sqlite-wal").read_bytes() == b"stale-wal"
    assert (analytics_quarantine / "radar.sqlite-shm").read_bytes() == b"stale-shm"
    assert repo_data not in analytics_quarantine.parents
    audit = json.loads(Path(restored.audit_path or "").read_text())
    assert audit["status"] == "completed"
    assert pending_restore_count(repo_data, REPO) == 0


def test_tampered_backup_fails_before_restore_changes_target(tmp_path):
    repo_data, paths, analytics = _source(tmp_path)
    output = tmp_path / "backup"
    create_backup(
        repo_data_dir=repo_data,
        analytics_path=analytics,
        repo=REPO,
        output=output,
        dry_run=False,
        services_stopped=True,
        created_at=NOW,
    )
    original = paths.state.read_bytes()
    (output / "payload" / "repo" / "live_state.json").write_text("tampered")

    with pytest.raises(BackupError, match="failed validation"):
        restore_backup(
            repo_data_dir=repo_data,
            analytics_path=analytics,
            repo=REPO,
            backup_path=output,
            dry_run=False,
            services_stopped=True,
            confirm_repo=REPO,
            restored_at="2026-08-24T03:00:00Z",
        )
    assert paths.state.read_bytes() == original
    assert pending_restore_count(repo_data, REPO) == 0


def test_unmanifested_payload_is_rejected_before_restore(tmp_path):
    repo_data, paths, analytics = _source(tmp_path)
    output = tmp_path / "backup"
    create_backup(
        repo_data_dir=repo_data,
        analytics_path=analytics,
        repo=REPO,
        output=output,
        dry_run=False,
        services_stopped=True,
        created_at=NOW,
    )
    extra = output / "payload" / "repo" / "not-in-manifest.json"
    extra.write_text("extra", encoding="utf-8")
    original = paths.state.read_bytes()

    with pytest.raises(BackupError, match="payload set"):
        restore_backup(
            repo_data_dir=repo_data,
            analytics_path=analytics,
            repo=REPO,
            backup_path=output,
            dry_run=False,
            services_stopped=True,
            confirm_repo=REPO,
            restored_at="2026-08-24T03:00:00Z",
        )
    assert paths.state.read_bytes() == original
