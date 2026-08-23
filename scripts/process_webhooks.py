"""Process the durable GitHub webhook inbox into live graph state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.events import EventLog
from issue_graphrag.live.github_api import GitHubClient
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.processor import DeliveryProcessor, ProcessingResult, run_worker_loop
from issue_graphrag.live.repositories import RepoRegistry, repo_paths
from issue_graphrag.live.runtime import configured_extractor
from issue_graphrag.live.timeutil import now_utc, to_iso


def _print_result(result: ProcessingResult) -> None:
    if result.status == "succeeded" and result.delta is not None:
        delta = result.delta
        changed = len(delta.fact_changes)
        extracted = len(delta.reextracted_documents)
        print(
            f"[{result.delivery_id}] succeeded: {delta.event_type}.{delta.action}, "
            f"{changed} fact changes, {extracted} documents extracted"
        )
        return
    print(f"[{result.delivery_id}] {result.status}: {result.error}")


def _print_worker_error(error: Exception) -> None:
    print(
        f"Worker iteration failed; retrying: {type(error).__name__}: {error}",
        file=sys.stderr,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/name; defaults to GITHUB_WEBHOOK_REPO")
    parser.add_argument("--inbox", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--event-log", type=Path, default=None)
    extraction = parser.add_mutually_exclusive_group()
    extraction.add_argument("--rules", type=Path, help="deterministic fixture extraction rules")
    extraction.add_argument("--llm", action="store_true", help="use the configured live LLM")
    parser.add_argument("--no-pr-files", action="store_true", help="skip PR files REST hydration")
    parser.add_argument("--once", action="store_true", help="process at most one ready delivery")
    parser.add_argument("--status", action="store_true", help="show inbox counts and dead letters")
    parser.add_argument(
        "--retry-failed", action="store_true", help="requeue all dead letters first"
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--lease-seconds", type=int, default=300)
    parser.add_argument("--retry-delay-seconds", type=int, default=30)
    parser.add_argument("--max-attempts", type=int, default=5)
    args = parser.parse_args()

    settings = load_settings()
    if args.poll_seconds < 0:
        parser.error("--poll-seconds must be non-negative")
    if args.lease_seconds <= 0:
        parser.error("--lease-seconds must be positive")
    if args.retry_delay_seconds < 0:
        parser.error("--retry-delay-seconds must be non-negative")
    if args.max_attempts <= 0:
        parser.error("--max-attempts must be positive")

    repo = args.repo or settings.github_webhook_repo
    if not repo:
        parser.error("--repo or GITHUB_WEBHOOK_REPO is required")
    registry = RepoRegistry(settings.repo_data_dir, settings.github_repos)
    repo_storage = (
        repo_paths(settings.repo_data_dir, repo) if args.status else registry.register(repo)
    )
    repo = repo_storage.repo
    inbox_path = args.inbox or repo_storage.inbox
    state_path = args.state or repo_storage.state
    log_path = args.event_log or repo_storage.event_log
    inbox = DeliveryInbox(inbox_path)

    if args.status:
        for status in ("pending", "processing", "succeeded", "failed"):
            print(f"{status:>10}: {inbox.count(status)}")
        for delivery in inbox.list_deliveries("failed"):
            print(
                f"  [{delivery.event.delivery_id}] attempts={delivery.attempts} "
                f"{delivery.last_error or '(no error recorded)'}"
            )
        return

    if args.retry_failed:
        count = inbox.retry_failed(to_iso(now_utc()))
        print(f"Requeued {count} failed deliveries")

    processor = DeliveryProcessor(
        repo=repo,
        inbox=inbox,
        state_path=state_path,
        event_log=EventLog(log_path),
        extractor=configured_extractor(rules=args.rules, use_llm=args.llm),
        github=None if args.no_pr_files else GitHubClient(token=settings.github_token),
        lease_seconds=args.lease_seconds,
        retry_delay_seconds=args.retry_delay_seconds,
        max_attempts=args.max_attempts,
        freshness_path=repo_storage.freshness,
    )

    if args.once:
        result = processor.process_one()
        if result is None:
            print("No ready deliveries")
            return
        _print_result(result)
        raise SystemExit(0 if result.status == "succeeded" else 1)

    print(f"Processing {inbox_path} for {repo}; state: {state_path}")
    try:
        run_worker_loop(
            processor,
            poll_seconds=args.poll_seconds,
            on_result=_print_result,
            on_error=_print_worker_error,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
