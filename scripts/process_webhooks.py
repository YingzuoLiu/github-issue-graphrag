"""Process the durable GitHub webhook inbox into live graph state."""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.events import EventLog
from issue_graphrag.live.github_api import GitHubClient
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.operations import install_shutdown_handlers
from issue_graphrag.live.processor import DeliveryProcessor, ProcessingResult, run_worker_loop
from issue_graphrag.live.repositories import RepoRegistry, repo_paths
from issue_graphrag.live.runtime import configured_extractor, validate_openrouter_operations
from issue_graphrag.live.extraction import LLMExtractor
from issue_graphrag.live.semantic_operations import (
    BatchPolicy,
    ExtractionCache,
    QuotaLedger,
    QuotaPolicy,
    SemanticBatchRunner,
)
from issue_graphrag.live.timeutil import now_utc, to_iso


def _print_result(result: ProcessingResult) -> None:
    if result.work_type == "semantic":
        suffix = f": {result.error}" if result.error else ""
        print(f"[semantic {result.document_id}] {result.status}{suffix}")
        return
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
        semantic_jobs = inbox.list_semantic_jobs(limit=100)
        print(f"  semantic: {len(semantic_jobs)} pending/deferred documents")
        for job in semantic_jobs:
            suffix = f" ({job.last_error})" if job.last_error else ""
            print(
                f"    [{job.document_id}] {job.status} "
                f"unit={job.next_unit_index}/{job.total_units}{suffix}"
            )
        print(
            "     cache: "
            f"{ExtractionCache(repo_storage.extraction_cache).count()} validated text units"
        )
        quota_summary = QuotaLedger(
            settings.repo_data_dir / "llm_operations.sqlite"
        ).usage_summary(to_iso(now_utc()))
        print(
            f"     quota: {quota_summary.daily_calls} calls, "
            f"{quota_summary.daily_input_tokens} input, "
            f"{quota_summary.daily_output_tokens} output on {quota_summary.utc_day}; "
            f"${quota_summary.monthly_cost_usd:.6f} in {quota_summary.utc_month}; "
            f"states={quota_summary.request_states or {'requests': 0}}"
        )
        return

    if args.retry_failed:
        count = inbox.retry_failed(to_iso(now_utc()))
        print(f"Requeued {count} failed deliveries")

    extractor = configured_extractor(rules=args.rules, use_llm=args.llm, operational=args.llm)
    semantic_runner = None
    if args.llm:
        try:
            validate_openrouter_operations(settings)
        except ValueError as exc:
            parser.error(str(exc))
        if not isinstance(extractor, LLMExtractor):
            raise RuntimeError("live LLM configuration did not produce an LLM extractor")
        semantic_runner = SemanticBatchRunner(
            repo=repo,
            extractor=extractor,
            cache=ExtractionCache(repo_storage.extraction_cache),
            quota=QuotaLedger(
                settings.repo_data_dir / "llm_operations.sqlite",
                QuotaPolicy(
                    daily_calls=settings.llm_daily_calls,
                    daily_input_tokens=settings.llm_daily_input_tokens,
                    daily_output_tokens=settings.llm_daily_output_tokens,
                    monthly_cost_usd=settings.llm_monthly_cost_usd,
                    bootstrap_calls=settings.llm_bootstrap_calls,
                    bootstrap_input_tokens=settings.llm_bootstrap_input_tokens,
                    bootstrap_output_tokens=settings.llm_bootstrap_output_tokens,
                    input_price_per_million_usd=settings.llm_input_price_per_million_usd,
                    output_price_per_million_usd=settings.llm_output_price_per_million_usd,
                    cost_safety_multiplier=settings.llm_cost_safety_multiplier,
                ),
            ),
            batch_policy=BatchPolicy(
                max_calls=settings.llm_batch_calls,
                max_input_tokens=settings.llm_batch_input_tokens,
                max_output_tokens=settings.llm_batch_output_tokens,
                max_output_tokens_per_call=settings.llm_max_output_tokens_per_call,
            ),
        )

    processor = DeliveryProcessor(
        repo=repo,
        inbox=inbox,
        state_path=state_path,
        event_log=EventLog(log_path),
        extractor=extractor,
        github=GitHubClient(token=settings.github_token),
        hydrate_pull_request_files=not args.no_pr_files,
        lease_seconds=args.lease_seconds,
        retry_delay_seconds=args.retry_delay_seconds,
        max_attempts=args.max_attempts,
        freshness_path=repo_storage.freshness,
        semantic_runner=semantic_runner,
    )

    if args.once:
        result = processor.process_one()
        if result is None:
            print("No ready deliveries")
            return
        _print_result(result)
        raise SystemExit(0 if result.status == "succeeded" else 1)

    print(f"Processing {inbox_path} for {repo}; state: {state_path}")
    stop_event = threading.Event()
    restore_handlers = install_shutdown_handlers(stop_event)
    try:
        run_worker_loop(
            processor,
            poll_seconds=args.poll_seconds,
            on_result=_print_result,
            on_error=_print_worker_error,
            should_stop=stop_event.is_set,
            wait=stop_event.wait,
        )
    finally:
        restore_handlers()


if __name__ == "__main__":
    main()
