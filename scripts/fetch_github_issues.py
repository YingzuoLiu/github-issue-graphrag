from __future__ import annotations

import argparse

from issue_graphrag.config import load_settings
from issue_graphrag.ingest.github_loader import fetch_issues, issues_to_documents
from issue_graphrag.storage.json_store import write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GitHub issues into local JSON.")
    parser.add_argument("repo", help="Repository in owner/name format, e.g. trustgraph-ai/trustgraph")
    parser.add_argument("--state", default="open", choices=["open", "closed", "all"])
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()

    settings = load_settings()
    issues = fetch_issues(args.repo, token=settings.github_token, state=args.state, per_page=args.limit)
    documents = issues_to_documents(args.repo, issues)

    output_path = settings.raw_data_dir / f"{args.repo.replace('/', '__')}_issues.json"
    write_json(output_path, [doc.model_dump() for doc in documents])
    print(f"Wrote {len(documents)} documents to {output_path}")


if __name__ == "__main__":
    main()
