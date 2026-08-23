"""Run the read-only contribution pilot against current public GitHub data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.pilot import (
    DEFAULT_CONSTRAINT_CONTRADICTION_THRESHOLD,
    DEFAULT_PILOT_REPOS,
    GitHubPilotClient,
    create_monitoring_run_directory,
    evaluate_snapshot,
    monitoring_run_id,
    render_markdown,
)
from issue_graphrag.live.timeutil import now_utc, to_iso


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
        "--constraint-contradiction-threshold",
        type=float,
        default=DEFAULT_CONSTRAINT_CONTRADICTION_THRESHOLD,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("eval/pilot_runs"),
        help="parent for immutable <UTC run id>/results.json and report.md outputs",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="optional explicit JSON path; must be paired with --markdown-output",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        help="optional explicit Markdown path; must be paired with --json-output",
    )
    args = parser.parse_args()

    if args.issue_limit < 1 or args.pull_limit < 1:
        parser.error("--issue-limit and --pull-limit must be positive")
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if not 0 <= args.comment_limit <= 100:
        parser.error("--comment-limit must be between 0 and 100")
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    if not 0 <= args.constraint_contradiction_threshold <= 1:
        parser.error("--constraint-contradiction-threshold must be between 0 and 1")
    if bool(args.json_output) != bool(args.markdown_output):
        parser.error("--json-output and --markdown-output must be provided together")

    run_started_at = to_iso(now_utc())
    run_id = monitoring_run_id(run_started_at)
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
            constraint_contradiction_threshold=args.constraint_contradiction_threshold,
        )
        results.append(result)
        rate = result["metrics"]["platform_constraint_contradiction_rate"]
        display = "n/a" if rate is None else f"{rate * 100:.1f}%"
        print(
            f"  {result['collection']['open_issues']} issues, "
            f"{result['collection']['open_pull_requests']} PRs, "
            f"{result['collection']['github_read_requests']} GETs, "
            f"constraint contradictions {display}"
        )

    write_requests = client.write_request_count
    envelope = {
        "evaluation": "real-repository-contribution-pilot-0",
        "run_id": run_id,
        "run_started_at": run_started_at,
        "read_only": write_requests == 0,
        "github_write_requests": write_requests,
        "configuration": {
            "repos": args.repos,
            "issue_limit": args.issue_limit,
            "pull_limit": args.pull_limit,
            "max_pages": args.max_pages,
            "comment_limit": args.comment_limit,
            "top_k": args.top_k,
            "constraint_contradiction_threshold": (
                args.constraint_contradiction_threshold
            ),
            "extractor": "deterministic GitHub facts only",
        },
        "results": results,
    }
    if args.json_output is not None and args.markdown_output is not None:
        json_output = args.json_output
        markdown_output = args.markdown_output
        if json_output.exists() or markdown_output.exists():
            parser.error("explicit output paths must not already exist")
        json_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
    else:
        try:
            run_directory = create_monitoring_run_directory(
                args.output_dir, run_started_at
            )
        except FileExistsError:
            parser.error(
                f"monitoring run directory already exists: {args.output_dir / run_id}"
            )
        json_output = run_directory / "results.json"
        markdown_output = run_directory / "report.md"

    json_output.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(
        render_markdown(results, top_k=args.top_k),
        encoding="utf-8",
    )
    print(f"Wrote immutable monitoring run {run_id}: {json_output} and {markdown_output}")

    failures: list[str] = []
    if write_requests:
        failures.append(f"measured {write_requests} GitHub write request(s)")
    for result in results:
        checks = result["engineering_checks"]
        for key in (
            "constraint_contradiction_rate_pass",
            "all_non_available_results_have_causal_evidence_url",
            "github_write_requests_are_zero",
        ):
            if not checks[key]:
                failures.append(f"{result['repo']}: {key}=false")
    if failures:
        raise SystemExit("Pilot engineering gate failed: " + "; ".join(failures))


if __name__ == "__main__":
    main()
