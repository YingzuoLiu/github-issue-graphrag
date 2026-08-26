"""Poll one public GitHub repository and enqueue deterministic reconciliation deliveries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.repositories import RepoRegistry, read_freshness, repo_paths
from issue_graphrag.live.synchronizer import (
    ConditionalGitHubClient,
    ScheduledSynchronizer,
    SyncConfig,
    SyncResult,
    run_synchronizer_loop,
)
from issue_graphrag.live.sync_checkpoint import (
    DEFAULT_CLOSED_RETENTION_SECONDS,
    DEFAULT_MAX_CHECKPOINT_BYTES,
    DEFAULT_MAX_CHECKPOINT_RESOURCES,
    DEFAULT_OPEN_RETENTION_SECONDS,
    CheckpointInspection,
    CheckpointRecoveryResult,
    RecoveryAction,
    SyncCheckpointError,
    inspect_sync_checkpoint,
    recover_sync_checkpoint,
)

DEFAULT_SYNC_REPO = "getzep/graphiti"


def _print_result(result: SyncResult) -> None:
    suffix = f": {result.error}" if result.error else ""
    print(
        f"[{result.repo}] {result.status}: {result.read_requests} GETs, "
        f"{result.not_modified_requests} not modified, "
        f"{result.planned_deliveries} planned, {result.enqueued} enqueued, "
        f"{result.duplicates} duplicate; checkpoint {result.checkpoint_resources} resources/"
        f"{result.checkpoint_bytes} bytes, compacted {result.compacted_resources}; "
        f"next {result.next_sync_at}{suffix}"
    )


def _write_report(path: Path, result: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _print_checkpoint_status(status: CheckpointInspection, freshness) -> None:  # noqa: ANN001
    print(f"repository: {status.repo}")
    print(f"source status: {freshness.source_status if freshness else 'unavailable'}")
    print(
        "last successful sync: "
        f"{freshness.last_source_sync_at if freshness and freshness.last_source_sync_at else 'not recorded'}"
    )
    print(
        "next sync: "
        f"{freshness.next_source_sync_at if freshness and freshness.next_source_sync_at else 'not scheduled'}"
    )
    print(
        f"last error: {freshness.source_error if freshness and freshness.source_error else 'none'}"
    )
    print(f"checkpoint state: {status.state}")
    print(f"checkpoint version: {status.on_disk_version or 'none'}")
    print(f"checkpoint resources: {status.resources}/{status.max_resources}")
    print(f"checkpoint bytes: {status.bytes}/{status.max_bytes}")
    print(f"checkpoint families: {status.families}")
    print(f"resource kinds: {json.dumps(status.resource_kinds, sort_keys=True)}")
    print(f"conditional responses: {status.conditional_responses}")
    print(f"last observed: {status.last_observed_at or 'not recorded'}")
    print(f"last compacted: {status.last_compacted_at or 'not recorded'}")
    print(
        "compacted total: "
        f"{status.compacted_resources_total} resources/"
        f"{status.compacted_families_total} families"
    )
    print(f"last-good state: {status.last_good_state} ({status.last_good_bytes} bytes)")
    print(f"quarantine files: {status.quarantine_files}")
    print(f"recovery records: {status.recovery_records}")
    print(f"pending recoveries: {status.pending_recoveries}")
    print(
        "latest recovery: "
        f"{status.latest_recovery_status or 'none'}"
        f" at {status.latest_recovery_at or 'not recorded'}"
    )
    if status.error:
        print(f"checkpoint error: {status.error}")
    if status.last_good_error:
        print(f"last-good error: {status.last_good_error}")


def _print_recovery(result: CheckpointRecoveryResult) -> None:
    print(f"repository: {result.repo}")
    print(f"recovery action: {result.action}")
    print(f"outcome: {result.outcome}")
    print(f"primary: {result.primary_path}")
    print(f"source: {result.source_path or 'empty v2 baseline'}")
    print(f"quarantine: {result.quarantine_path or 'not required'}")
    print(f"audit: {result.audit_path or 'not written'}")
    print(f"warning: {result.warning}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repo",
        nargs="?",
        default=DEFAULT_SYNC_REPO,
        help=f"owner/name; defaults to {DEFAULT_SYNC_REPO}",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run one poll (the default)")
    mode.add_argument("--loop", action="store_true", help="run the fixed scheduled loop")
    mode.add_argument("--status", action="store_true", help="show source checkpoint status")
    mode.add_argument(
        "--recover-checkpoint",
        action="store_true",
        help="restore the verified last-good checkpoint",
    )
    mode.add_argument(
        "--rebaseline-checkpoint",
        action="store_true",
        help="quarantine the primary and install an empty v2 checkpoint",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="describe checkpoint recovery without changing files",
    )
    parser.add_argument(
        "--confirm-repo",
        help="exact owner/name confirmation required for mutating recovery",
    )
    parser.add_argument(
        "--expect-noop", action="store_true", help="fail unless no delivery is planned"
    )
    parser.add_argument("--json-output", type=Path, help="write a JSON operational summary")
    parser.add_argument("--interval-seconds", type=int)
    parser.add_argument("--item-limit", type=int, default=30)
    parser.add_argument("--item-max-pages", type=int, default=10)
    parser.add_argument("--comment-limit-per-item", type=int, default=300)
    parser.add_argument("--comment-max-pages", type=int, default=10)
    parser.add_argument("--file-limit-per-pull", type=int, default=3000)
    parser.add_argument("--file-max-pages", type=int, default=30)
    parser.add_argument("--dependency-limit-per-issue", type=int, default=1000)
    parser.add_argument("--dependency-max-pages", type=int, default=10)
    parser.add_argument("--http-attempts", type=int, default=3)
    parser.add_argument("--http-backoff-seconds", type=float, default=1.0)
    parser.add_argument(
        "--checkpoint-open-retention-seconds",
        type=int,
        default=DEFAULT_OPEN_RETENTION_SECONDS,
    )
    parser.add_argument(
        "--checkpoint-closed-retention-seconds",
        type=int,
        default=DEFAULT_CLOSED_RETENTION_SECONDS,
    )
    parser.add_argument(
        "--checkpoint-max-resources",
        type=int,
        default=DEFAULT_MAX_CHECKPOINT_RESOURCES,
    )
    parser.add_argument(
        "--checkpoint-max-bytes",
        type=int,
        default=DEFAULT_MAX_CHECKPOINT_BYTES,
    )
    args = parser.parse_args()

    recovery_mode = args.recover_checkpoint or args.rebaseline_checkpoint
    if args.loop and (args.expect_noop or args.json_output):
        parser.error("--expect-noop and --json-output are single-run options")
    if (args.dry_run or args.confirm_repo) and not recovery_mode:
        parser.error("--dry-run and --confirm-repo apply only to checkpoint recovery")
    if (args.status or recovery_mode) and args.expect_noop:
        parser.error("--expect-noop applies only to a synchronization poll")

    settings = load_settings()
    interval_seconds = (
        settings.github_sync_interval_seconds
        if args.interval_seconds is None
        else args.interval_seconds
    )
    try:
        config = SyncConfig(
            interval_seconds=interval_seconds,
            item_limit=args.item_limit,
            item_max_pages=args.item_max_pages,
            comment_limit_per_item=args.comment_limit_per_item,
            comment_max_pages=args.comment_max_pages,
            file_limit_per_pull=args.file_limit_per_pull,
            file_max_pages=args.file_max_pages,
            dependency_limit_per_issue=args.dependency_limit_per_issue,
            dependency_max_pages=args.dependency_max_pages,
            http_attempts=args.http_attempts,
            http_backoff_seconds=args.http_backoff_seconds,
            checkpoint_open_retention_seconds=args.checkpoint_open_retention_seconds,
            checkpoint_closed_retention_seconds=args.checkpoint_closed_retention_seconds,
            checkpoint_max_resources=args.checkpoint_max_resources,
            checkpoint_max_bytes=args.checkpoint_max_bytes,
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.status:
        paths = repo_paths(settings.repo_data_dir, args.repo)
        try:
            freshness = read_freshness(paths.freshness, paths.repo)
        except Exception as exc:
            freshness = None
            print(f"freshness error: {type(exc).__name__}: {exc}")
        checkpoint = inspect_sync_checkpoint(
            paths.sync_state,
            paths.repo,
            config.checkpoint_policy,
        )
        _print_checkpoint_status(checkpoint, freshness)
        if args.json_output:
            _write_report(args.json_output, checkpoint)
        recovery_required = (
            checkpoint.state == "corrupt"
            or checkpoint.pending_recoveries
            or (checkpoint.state == "missing" and checkpoint.last_good_state != "missing")
        )
        if recovery_required:
            raise SystemExit(2)
        return

    if recovery_mode:
        paths = repo_paths(settings.repo_data_dir, args.repo)
        action: RecoveryAction = (
            "restore_last_good" if args.recover_checkpoint else "rebaseline"
        )
        try:
            recovery = recover_sync_checkpoint(
                paths.sync_state,
                paths.repo,
                action=action,
                dry_run=args.dry_run,
                confirm_repo=args.confirm_repo,
            )
        except SyncCheckpointError as exc:
            raise SystemExit(f"checkpoint recovery refused: {exc}") from None
        _print_recovery(recovery)
        if args.json_output:
            _write_report(args.json_output, recovery)
        return

    paths = RepoRegistry(settings.repo_data_dir, settings.github_repos).register(args.repo)
    synchronizer = ScheduledSynchronizer(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        sync_state_path=paths.sync_state,
        freshness_path=paths.freshness,
        observer=ConditionalGitHubClient(token=settings.github_token),
        config=config,
    )

    if args.loop:
        try:
            run_synchronizer_loop(synchronizer, _print_result)
        except KeyboardInterrupt:
            pass
        return

    result = synchronizer.sync_once()
    _print_result(result)
    if args.json_output:
        _write_report(args.json_output, result)
    if result.status != "succeeded":
        raise SystemExit(1)
    if args.expect_noop and result.planned_deliveries:
        raise SystemExit(
            f"expected no reconciliation delivery, observed {result.planned_deliveries}"
        )


if __name__ == "__main__":
    main()
