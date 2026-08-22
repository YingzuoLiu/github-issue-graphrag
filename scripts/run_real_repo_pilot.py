"""Run the read-only contribution pilot against current public GitHub data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.pilot import (
    DEFAULT_FALSE_AVAILABLE_THRESHOLD,
    DEFAULT_PILOT_REPOS,
    GitHubPilotClient,
    evaluate_snapshot,
    render_markdown,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "repos",
        nargs="*",
        default=list(DEFAULT_PILOT_REPOS),
        help="owner/name repositories; defaults to the three documented pilot repositories",
    )
    parser.add_argument("--issue-limit", type=int, default=50)
    parser.add_argument("--pull-limit", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--comment-limit", type=int, default=100)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--false-available-threshold",
        type=float,
        default=DEFAULT_FALSE_AVAILABLE_THRESHOLD,
    )
    parser.add_argument("--json-output", type=Path, default=Path("eval/pilot_results.json"))
    parser.add_argument("--markdown-output", type=Path, default=Path("eval/pilot_results.md"))
    args = parser.parse_args()

    if args.issue_limit < 1 or args.pull_limit < 1:
        parser.error("--issue-limit and --pull-limit must be positive")
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if not 0 <= args.comment_limit <= 100:
        parser.error("--comment-limit must be between 0 and 100")
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    if not 0 <= args.false_available_threshold <= 1:
        parser.error("--false-available-threshold must be between 0 and 1")

    settings = load_settings()
    client = GitHubPilotClient(token=settings.github_token)
    results = []
    for repo in args.repos:
        print(f"Fetching {repo} (GET only)...")
        snapshot = client.fetch_snapshot(
            repo,
            issue_limit=args.issue_limit,
            pull_limit=args.pull_limit,
            max_pages=args.max_pages,
            comment_limit=args.comment_limit,
        )
        result = evaluate_snapshot(
            snapshot,
            top_k=args.top_k,
            false_available_threshold=args.false_available_threshold,
        )
        results.append(result)
        rate = result["metrics"]["false_available_rate"]
        display = "n/a" if rate is None else f"{rate * 100:.1f}%"
        print(
            f"  {result['collection']['open_issues']} issues, "
            f"{result['collection']['open_pull_requests']} PRs, "
            f"{result['collection']['github_read_requests']} GETs, "
            f"false-available {display}"
        )

    envelope = {
        "evaluation": "real-repository-contribution-pilot-0",
        "read_only": True,
        "github_write_requests": 0,
        "configuration": {
            "repos": args.repos,
            "issue_limit": args.issue_limit,
            "pull_limit": args.pull_limit,
            "max_pages": args.max_pages,
            "comment_limit": args.comment_limit,
            "top_k": args.top_k,
            "false_available_threshold": args.false_available_threshold,
            "extractor": "deterministic GitHub facts only",
        },
        "results": results,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(
        render_markdown(results, top_k=args.top_k),
        encoding="utf-8",
    )
    print(f"Wrote {args.json_output} and {args.markdown_output}")


if __name__ == "__main__":
    main()
