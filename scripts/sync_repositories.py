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
    read_sync_state,
    run_synchronizer_loop,
)

DEFAULT_SYNC_REPO = "getzep/graphiti"


def _print_result(result: SyncResult) -> None:
    suffix = f": {result.error}" if result.error else ""
    print(
        f"[{result.repo}] {result.status}: {result.read_requests} GETs, "
        f"{result.not_modified_requests} not modified, "
        f"{result.planned_deliveries} planned, {result.enqueued} enqueued, "
        f"{result.duplicates} duplicate; next {result.next_sync_at}{suffix}"
    )


def _write_report(path: Path, result: SyncResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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
    parser.add_argument("--status", action="store_true", help="show source checkpoint status")
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
    args = parser.parse_args()

    if args.loop and (args.expect_noop or args.json_output):
        parser.error("--expect-noop and --json-output are single-run options")

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
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.status:
        paths = repo_paths(settings.repo_data_dir, args.repo)
        freshness = read_freshness(paths.freshness, paths.repo)
        checkpoint = read_sync_state(paths.sync_state, paths.repo)
        print(f"repository: {paths.repo}")
        print(f"source status: {freshness.source_status}")
        print(f"last successful sync: {freshness.last_source_sync_at or 'not recorded'}")
        print(f"next sync: {freshness.next_source_sync_at or 'not scheduled'}")
        print(f"last error: {freshness.source_error or 'none'}")
        print(f"checkpoint resources: {len(checkpoint.resources)}")
        print(f"conditional responses: {len(checkpoint.request_cache)}")
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
