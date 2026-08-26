from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import issue_graphrag.live.sync_checkpoint as checkpoint_module
from issue_graphrag.live.repositories import repo_paths
from issue_graphrag.live.models import LiveState, RepoEvent
from issue_graphrag.live.records import UnsupportedEvent, apply_event_to_records
from issue_graphrag.live.sync_checkpoint import (
    CheckpointLimitExceeded,
    CheckpointPolicy,
    RecoveryConfirmationError,
    RepoSyncState,
    SyncCheckpointError,
    SyncResource,
    compact_sync_resources,
    inspect_sync_checkpoint,
    read_sync_state,
    recover_sync_checkpoint,
    sync_checkpoint_paths,
    validate_checkpoint_limits,
    write_sync_state,
)
from issue_graphrag.live.timeutil import to_iso

REPO = "getzep/graphiti"
NOW = "2026-08-24T02:00:00Z"
LATER = "2026-08-24T02:15:00Z"


def _issue(
    number: int,
    *,
    state: str = "open",
    observed_at: str = NOW,
    body_size: int = 0,
) -> SyncResource:
    return SyncResource.observed(
        kind="issue",
        identity=f"issue:{number}",
        source_updated_at=observed_at,
        last_observed_at=observed_at,
        payload={
            "number": number,
            "title": f"Issue {number}",
            "body": "x" * body_size,
            "state": state,
            "updated_at": observed_at,
            "closed_at": observed_at if state == "closed" else None,
        },
        parent_kind="issue",
        parent_number=number,
    )


def _comment(number: int, comment_id: int, *, observed_at: str = NOW) -> SyncResource:
    return SyncResource.observed(
        kind="comment",
        identity=f"comment:{comment_id}",
        source_updated_at=observed_at,
        last_observed_at=observed_at,
        payload={"id": str(comment_id), "body": "evidence", "updated_at": observed_at},
        parent_kind="issue",
        parent_number=number,
    )


def _manifest(number: int, ids: list[int], *, observed_at: str = NOW) -> SyncResource:
    return SyncResource.observed(
        kind="comment_manifest",
        identity=f"comment_manifest:issue:{number}",
        source_updated_at=observed_at,
        last_observed_at=observed_at,
        payload={},
        attachments={"comment_ids": [str(comment_id) for comment_id in sorted(ids)]},
        parent_kind="issue",
        parent_number=number,
    )


def _source_event(
    delivery_id: str,
    event_type: str,
    action: str,
    received_at: str,
    payload: dict,
    attachments: dict | None = None,
) -> RepoEvent:
    return RepoEvent(
        delivery_id=delivery_id,
        event_type=event_type,
        action=action,
        repo=REPO,
        received_at=received_at,
        payload={
            "action": action,
            "repository": {"full_name": REPO},
            **payload,
        },
        attachments=attachments or {},
        source="reconciliation",
    )


def test_v1_checkpoint_migrates_in_memory_and_next_commit_is_v2(tmp_path):
    path = tmp_path / "sync_state.json"
    resource = _issue(7)
    payload = {
        "version": 1,
        "repo": REPO,
        "last_observed_at": NOW,
        "request_cache": {},
        "resources": {resource.identity: resource.model_dump(exclude={"last_observed_at"})},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    migrated = read_sync_state(path, REPO)

    assert migrated.version == 2
    assert migrated.resources["issue:7"].last_observed_at == NOW
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1

    write_sync_state(path, migrated)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 2
    assert (
        json.loads(sync_checkpoint_paths(path).last_good.read_text(encoding="utf-8"))["version"]
        == 1
    )


def test_compaction_uses_closed_and_open_lifecycles_and_never_splits_a_family():
    closed = [_issue(1, state="closed"), _comment(1, 101), _manifest(1, [101])]
    open_rows = [_issue(2), _comment(2, 201), _manifest(2, [201])]
    resources = {row.identity: row for row in [*closed, *open_rows]}
    policy = CheckpointPolicy(
        closed_retention_seconds=60,
        open_retention_seconds=3600,
        max_resources=20,
        max_bytes=1_000_000,
    )

    compacted = compact_sync_resources(
        resources,
        observed_identities=frozenset(),
        compacted_at="2026-08-24T02:02:00Z",
        policy=policy,
    )

    assert compacted.compacted_families == 1
    assert compacted.compacted_resources == 3
    assert set(compacted.resources) == {"issue:2", "comment:201", "comment_manifest:issue:2"}


def test_an_observed_parent_protects_its_whole_incomplete_comment_family():
    rows = [_issue(1, state="closed"), _comment(1, 101), _manifest(1, [101])]
    resources = {row.identity: row for row in rows}
    policy = CheckpointPolicy(
        closed_retention_seconds=0,
        open_retention_seconds=0,
        max_resources=20,
        max_bytes=1_000_000,
    )

    compacted = compact_sync_resources(
        resources,
        observed_identities=frozenset({"issue:1"}),
        compacted_at=LATER,
        policy=policy,
    )

    assert compacted.compacted_families == 0
    assert set(compacted.resources) == {row.identity for row in rows}


def test_comment_manifest_and_included_edit_converge_in_either_inbox_order():
    issue = {
        "number": 7,
        "title": "Issue 7",
        "body": "",
        "state": "open",
        "updated_at": NOW,
    }
    initial = LiveState(repo=REPO)
    apply_event_to_records(
        initial,
        _source_event("issue", "issues", "reconciled", NOW, {"issue": issue}),
    )
    for comment_id in (91, 92):
        apply_event_to_records(
            initial,
            _source_event(
                f"comment-{comment_id}",
                "issue_comment",
                "created",
                NOW,
                {
                    "issue": {"number": 7},
                    "comment": {
                        "id": comment_id,
                        "body": "old",
                        "updated_at": NOW,
                    },
                },
            ),
        )
    edited = _source_event(
        "comment-91-edit",
        "issue_comment",
        "edited",
        "2026-08-24T02:10:00Z",
        {
            "issue": {"number": 7},
            "comment": {
                "id": 91,
                "body": "new",
                "updated_at": "2026-08-24T02:10:00Z",
            },
        },
    )
    manifest = _source_event(
        "manifest-91",
        "issue_comments",
        "reconciled",
        LATER,
        {"issue": {"number": 7}},
        {"comment_ids": ["91"]},
    )

    results = []
    for order in ((edited, manifest), (manifest, edited)):
        state = initial.model_copy(deep=True)
        for event in order:
            apply_event_to_records(state, event)
        results.append(state)

    assert results[0] == results[1]
    item = results[0].items[f"{REPO}#issue-7"]
    assert item.comments["91"].body == "new"
    assert "92" not in item.comments and "92" in item.deleted_comments


def test_comment_manifest_is_not_an_external_webhook_control_surface():
    event = _source_event(
        "manifest",
        "issue_comments",
        "reconciled",
        LATER,
        {"issue": {"number": 7}},
        {"comment_ids": []},
    ).model_copy(update={"source": "webhook"})

    with pytest.raises(UnsupportedEvent, match="internal reconciliation event"):
        apply_event_to_records(LiveState(repo=REPO), event)


def test_long_cycle_fixture_remains_bounded_under_continuous_churn():
    policy = CheckpointPolicy(
        closed_retention_seconds=120,
        open_retention_seconds=120,
        max_resources=8,
        max_bytes=1_000_000,
    )
    start = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    resources: dict[str, SyncResource] = {}
    compacted_families = 0

    for offset in range(200):
        observed_at = to_iso(start + timedelta(minutes=offset))
        issue = _issue(offset, state="closed", observed_at=observed_at)
        manifest = _manifest(offset, [], observed_at=observed_at)
        resources[issue.identity] = issue
        resources[manifest.identity] = manifest
        result = compact_sync_resources(
            resources,
            observed_identities=frozenset({issue.identity, manifest.identity}),
            compacted_at=observed_at,
            policy=policy,
        )
        resources = result.resources
        compacted_families += result.compacted_families
        validate_checkpoint_limits(
            RepoSyncState(repo=REPO, last_observed_at=observed_at, resources=resources),
            policy,
        )
        assert len(resources) <= 6

    assert compacted_families == 198
    assert set(resources) == {
        "issue:198",
        "comment_manifest:issue:198",
        "issue:199",
        "comment_manifest:issue:199",
    }


def test_resource_and_byte_limits_fail_closed():
    with pytest.raises(CheckpointLimitExceeded, match="resource limit"):
        validate_checkpoint_limits(
            RepoSyncState(repo=REPO, resources={"issue:1": _issue(1), "issue:2": _issue(2)}),
            CheckpointPolicy(max_resources=1, max_bytes=1_000_000),
        )

    with pytest.raises(CheckpointLimitExceeded, match="byte limit"):
        validate_checkpoint_limits(
            RepoSyncState(repo=REPO, resources={"issue:1": _issue(1, body_size=5000)}),
            CheckpointPolicy(max_resources=10, max_bytes=1000),
        )


def test_last_good_is_verified_and_corrupt_primary_cannot_be_silently_overwritten(tmp_path):
    path = tmp_path / "sync_state.json"
    first = RepoSyncState(repo=REPO, last_observed_at=NOW, resources={"issue:1": _issue(1)})
    second = RepoSyncState(
        repo=REPO,
        last_observed_at=LATER,
        resources={"issue:2": _issue(2, observed_at=LATER)},
    )
    write_sync_state(path, first)
    write_sync_state(path, second)
    checkpoint_paths = sync_checkpoint_paths(path)
    assert set(read_sync_state(checkpoint_paths.last_good, REPO).resources) == {"issue:1"}

    path.write_text('{"version": 2, "broken":', encoding="utf-8")
    corrupt_bytes = path.read_bytes()
    with pytest.raises(SyncCheckpointError, match="explicit recovery"):
        write_sync_state(path, RepoSyncState(repo=REPO))
    assert path.read_bytes() == corrupt_bytes


def test_missing_primary_with_last_good_requires_explicit_recovery(tmp_path):
    path = tmp_path / "sync_state.json"
    original = RepoSyncState(repo=REPO, resources={"issue:1": _issue(1)})
    write_sync_state(path, original)
    path.unlink()

    with pytest.raises(SyncCheckpointError, match="primary checkpoint is missing"):
        read_sync_state(path, REPO)
    status = inspect_sync_checkpoint(path, REPO)
    assert status.state == "missing" and status.last_good_state == "healthy"

    recover_sync_checkpoint(
        path,
        REPO,
        action="restore_last_good",
        dry_run=False,
        confirm_repo=REPO,
        recovered_at="2026-08-24T02:45:00Z",
    )
    assert set(read_sync_state(path, REPO).resources) == {"issue:1"}


def test_corruption_status_dry_run_restore_quarantine_and_audit(tmp_path):
    path = tmp_path / "sync_state.json"
    first = RepoSyncState(repo=REPO, last_observed_at=NOW, resources={"issue:1": _issue(1)})
    second = RepoSyncState(
        repo=REPO,
        last_observed_at=LATER,
        resources={"issue:2": _issue(2, observed_at=LATER)},
    )
    write_sync_state(path, first)
    write_sync_state(path, second)
    corrupt_bytes = b'{"version": 2, "repo": "getzep/graphiti", "resources": {'
    path.write_bytes(corrupt_bytes)

    status = inspect_sync_checkpoint(path, REPO)
    assert status.state == "corrupt"
    assert status.last_good_state == "healthy"
    assert "invalid checkpoint JSON" in (status.error or "")

    dry_run = recover_sync_checkpoint(
        path,
        REPO,
        action="restore_last_good",
        dry_run=True,
        recovered_at="2026-08-24T03:00:00Z",
    )
    assert dry_run.outcome == "dry_run"
    assert path.read_bytes() == corrupt_bytes
    assert not sync_checkpoint_paths(path).quarantine.exists()

    with pytest.raises(RecoveryConfirmationError, match="--confirm-repo"):
        recover_sync_checkpoint(
            path,
            REPO,
            action="restore_last_good",
            dry_run=False,
            confirm_repo="another/repo",
            recovered_at="2026-08-24T03:00:00Z",
        )

    recovered = recover_sync_checkpoint(
        path,
        REPO,
        action="restore_last_good",
        dry_run=False,
        confirm_repo=REPO,
        recovered_at="2026-08-24T03:00:00Z",
    )

    assert recovered.outcome == "completed"
    assert set(read_sync_state(path, REPO).resources) == {"issue:1"}
    assert Path(recovered.quarantine_path or "").read_bytes() == corrupt_bytes
    audit = json.loads(Path(recovered.audit_path or "").read_text(encoding="utf-8"))
    assert audit["status"] == "completed"
    after = inspect_sync_checkpoint(path, REPO)
    assert after.state == "healthy"
    assert after.quarantine_files == 1
    assert after.recovery_records == 1
    assert after.pending_recoveries == 0
    assert after.latest_recovery_status == "completed"


def test_interrupted_recovery_remains_visible_as_planned(tmp_path, monkeypatch):
    path = tmp_path / "sync_state.json"
    write_sync_state(path, RepoSyncState(repo=REPO, resources={"issue:1": _issue(1)}))
    path.write_text("{broken", encoding="utf-8")
    paths = sync_checkpoint_paths(path)
    real_write = checkpoint_module._atomic_write_bytes

    def fail_quarantine(destination, payload):  # noqa: ANN001, ANN202
        if Path(destination).parent == paths.quarantine:
            raise OSError("simulated quarantine volume failure")
        return real_write(destination, payload)

    monkeypatch.setattr(checkpoint_module, "_atomic_write_bytes", fail_quarantine)
    with pytest.raises(OSError, match="quarantine volume failure"):
        recover_sync_checkpoint(
            path,
            REPO,
            action="restore_last_good",
            dry_run=False,
            confirm_repo=REPO,
            recovered_at="2026-08-24T03:10:00Z",
        )

    status = inspect_sync_checkpoint(path, REPO)
    assert status.state == "corrupt"
    assert status.recovery_records == 1
    assert status.pending_recoveries == 1
    assert status.latest_recovery_status == "planned"
    assert status.latest_recovery_at == "2026-08-24T03:10:00Z"


def test_fingerprint_corruption_is_reported_without_trusting_the_json_shape(tmp_path):
    path = tmp_path / "sync_state.json"
    write_sync_state(path, RepoSyncState(repo=REPO, resources={"issue:1": _issue(1)}))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["resources"]["issue:1"]["fingerprint"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = inspect_sync_checkpoint(path, REPO)

    assert status.state == "corrupt"
    assert status.last_good_state == "healthy"
    assert "fingerprint mismatch: issue:1" in (status.error or "")


def test_confirmed_rebaseline_preserves_corrupt_bytes_and_installs_empty_v2(tmp_path):
    path = tmp_path / "sync_state.json"
    corrupt_bytes = b"not-json\x00"
    path.write_bytes(corrupt_bytes)

    result = recover_sync_checkpoint(
        path,
        REPO,
        action="rebaseline",
        dry_run=False,
        confirm_repo=REPO,
        recovered_at="2026-08-24T03:30:00Z",
    )

    state = read_sync_state(path, REPO)
    assert state.version == 2 and state.resources == {}
    assert Path(result.quarantine_path or "").read_bytes() == corrupt_bytes
    assert "deletion is inferred only after a complete comment manifest" in result.warning


def test_last_good_can_explicitly_roll_back_a_healthy_rebaseline(tmp_path):
    path = tmp_path / "sync_state.json"
    original = RepoSyncState(repo=REPO, resources={"issue:1": _issue(1)})
    write_sync_state(path, original)
    recover_sync_checkpoint(
        path,
        REPO,
        action="rebaseline",
        dry_run=False,
        confirm_repo=REPO,
        recovered_at="2026-08-24T04:00:00Z",
    )
    assert read_sync_state(path, REPO).resources == {}

    rollback = recover_sync_checkpoint(
        path,
        REPO,
        action="restore_last_good",
        dry_run=False,
        confirm_repo=REPO,
        recovered_at="2026-08-24T04:01:00Z",
    )

    assert set(read_sync_state(path, REPO).resources) == {"issue:1"}
    assert Path(rollback.quarantine_path or "").exists()


def test_failed_primary_replace_leaves_the_previous_generation_readable(tmp_path, monkeypatch):
    path = tmp_path / "sync_state.json"
    first = RepoSyncState(repo=REPO, last_observed_at=NOW, resources={"issue:1": _issue(1)})
    second = RepoSyncState(
        repo=REPO,
        last_observed_at=LATER,
        resources={"issue:2": _issue(2, observed_at=LATER)},
    )
    write_sync_state(path, first)
    real_replace = checkpoint_module.os.replace

    def fail_primary(source, destination):  # noqa: ANN001, ANN202
        if Path(destination) == path:
            raise OSError("simulated power loss before primary replace")
        return real_replace(source, destination)

    monkeypatch.setattr(checkpoint_module.os, "replace", fail_primary)
    with pytest.raises(OSError, match="simulated power loss"):
        write_sync_state(path, second)

    assert set(read_sync_state(path, REPO).resources) == {"issue:1"}
    assert not list(tmp_path.glob(".sync_state.json.*.tmp"))


def test_status_cli_reports_corruption_without_a_traceback(tmp_path):
    paths = repo_paths(tmp_path, REPO)
    paths.root.mkdir(parents=True)
    paths.sync_state.write_text("{broken", encoding="utf-8")
    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["REPO_DATA_DIR"] = str(tmp_path)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(project_root / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    completed = subprocess.run(
        [sys.executable, "scripts/sync_repositories.py", REPO, "--status"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "checkpoint state: corrupt" in completed.stdout
    assert "checkpoint error:" in completed.stdout
    assert "Traceback" not in completed.stderr
