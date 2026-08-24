"""Fetch a live-index snapshot: issues, pull requests, comments and changed files.

Unlike scripts/fetch_github_issues.py, this keeps pull requests and comments,
because those are exactly what makes a contribution opportunity change.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.ingest.github_loader import build_live_seed
from issue_graphrag.live.repositories import RepoRegistry, read_freshness, write_freshness
from issue_graphrag.live.timeutil import now_utc, to_iso


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="owner/name, for example trustgraph-ai/trustgraph")
    parser.add_argument("--state", default="all", choices=["open", "closed", "all"])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--item-max-pages", type=int, default=10)
    parser.add_argument("--comment-limit-per-item", type=int, default=300)
    parser.add_argument("--comment-max-pages", type=int, default=10)
    parser.add_argument("--file-limit-per-pull", type=int, default=3000)
    parser.add_argument("--file-max-pages", type=int, default=30)
    parser.add_argument("--no-comments", action="store_true")
    parser.add_argument("--no-files", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")
    for name in ("item_max_pages", "comment_max_pages", "file_max_pages"):
        if getattr(args, name) < 1:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("comment_limit_per_item", "file_limit_per_pull"):
        if getattr(args, name) < 0:
            parser.error(f"--{name.replace('_', '-')} must be non-negative")

    settings = load_settings()
    repo_storage = RepoRegistry(settings.repo_data_dir, settings.github_repos).register(args.repo)
    fetched_at = to_iso(now_utc())
    snapshot = build_live_seed(
        repo_storage.repo,
        token=settings.github_token,
        state=args.state,
        limit=args.limit,
        with_comments=not args.no_comments,
        with_files=not args.no_files,
        item_max_pages=args.item_max_pages,
        comment_limit_per_item=args.comment_limit_per_item,
        comment_max_pages=args.comment_max_pages,
        file_limit_per_pull=args.file_limit_per_pull,
        file_max_pages=args.file_max_pages,
    )
    snapshot["fetched_at"] = fetched_at
    snapshot["backfill"] = {
        "item_limit": args.limit,
        "item_max_pages": args.item_max_pages,
        "comment_limit_per_item": args.comment_limit_per_item,
        "comment_max_pages": args.comment_max_pages,
        "file_limit_per_pull": args.file_limit_per_pull,
        "file_max_pages": args.file_max_pages,
    }

    output = args.output or repo_storage.bootstrap_seed
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)

    freshness = read_freshness(repo_storage.freshness, repo_storage.repo)
    freshness.last_source_sync_at = fetched_at
    freshness.last_source_attempt_at = fetched_at
    freshness.source_status = "current"
    freshness.source_kind = "bootstrap"
    freshness.source_error = None
    freshness.semantic_status = "pending"
    freshness.last_error = None
    write_freshness(repo_storage.freshness, freshness)

    issues = sum(1 for item in snapshot["items"] if item["kind"] == "issue")
    pulls = len(snapshot["items"]) - issues
    comments = sum(len(item["comments"]) for item in snapshot["items"])
    print(f"Wrote {output}")
    print(f"  {issues} issues, {pulls} pull requests, {comments} comments")


if __name__ == "__main__":
    main()
