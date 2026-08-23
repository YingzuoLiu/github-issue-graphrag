"""Review and optionally freeze a real Graphiti contribution snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.pilot import (
    GitHubPilotClient,
    contribution_regression_signature,
    snapshot_to_payload,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="getzep/graphiti")
    parser.add_argument("--issue-limit", type=int, default=25)
    parser.add_argument("--pull-limit", type=int, default=25)
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--comment-limit", type=int, default=50)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("tests/fixtures/contribution"),
    )
    parser.add_argument(
        "--accept",
        action="store_true",
        help="replace the reviewed snapshot and expected contract; default is a no-write preview",
    )
    args = parser.parse_args()

    settings = load_settings()
    client = GitHubPilotClient(token=settings.github_token)
    snapshot = client.fetch_snapshot(
        args.repo,
        issue_limit=args.issue_limit,
        pull_limit=args.pull_limit,
        max_pages=args.max_pages,
        comment_limit=args.comment_limit,
    )
    if snapshot.write_request_count:
        raise SystemExit(
            f"refusing to freeze a snapshot after {snapshot.write_request_count} GitHub writes"
        )

    parameters = {
        "issue_limit": args.issue_limit,
        "pull_limit": args.pull_limit,
        "max_pages": args.max_pages,
        "comment_limit": args.comment_limit,
        "extractor": "deterministic GitHub facts only",
    }
    payload = snapshot_to_payload(snapshot, collection_parameters=parameters)
    expected = contribution_regression_signature(snapshot)
    status_counts = {
        status: sum(row["status"] == status for row in expected["opportunities"])
        for status in ("available", "claimed", "blocked")
    }
    print(
        f"{snapshot.repo}: {len(snapshot.issues)} issues, {len(snapshot.pulls)} PRs, "
        f"{snapshot.request_count} GETs, 0 writes"
    )
    print(f"fingerprint: {snapshot.fingerprint}")
    print(f"opportunity statuses: {status_counts}")

    if not args.accept:
        print("Preview only; no fixture files written. Re-run with --accept after review.")
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = args.output_dir / "graphiti_snapshot.json"
    expected_path = args.output_dir / "graphiti_expected.json"
    snapshot_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    expected_path.write_text(
        json.dumps(expected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Accepted reviewed fixture: {snapshot_path} and {expected_path}")


if __name__ == "__main__":
    main()
