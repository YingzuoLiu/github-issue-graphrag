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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="owner/name, for example trustgraph-ai/trustgraph")
    parser.add_argument("--state", default="all", choices=["open", "closed", "all"])
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--no-comments", action="store_true")
    parser.add_argument("--no-files", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    settings = load_settings()
    snapshot = build_live_seed(
        args.repo,
        token=settings.github_token,
        state=args.state,
        limit=args.limit,
        with_comments=not args.no_comments,
        with_files=not args.no_files,
    )

    output = args.output or settings.raw_data_dir / f"{args.repo.replace('/', '__')}_live_seed.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)

    issues = sum(1 for item in snapshot["items"] if item["kind"] == "issue")
    pulls = len(snapshot["items"]) - issues
    comments = sum(len(item["comments"]) for item in snapshot["items"])
    print(f"Wrote {output}")
    print(f"  {issues} issues, {pulls} pull requests, {comments} comments")


if __name__ == "__main__":
    main()
