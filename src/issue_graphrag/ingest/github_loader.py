from __future__ import annotations

import re
from typing import Any

import requests

from issue_graphrag.models import SourceDocument


_REPO_PATTERN = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^/]+)$")


def parse_repo(repo: str) -> tuple[str, str]:
    match = _REPO_PATTERN.match(repo.strip())
    if not match:
        raise ValueError("repo must be in 'owner/name' format")
    return match.group("owner"), match.group("repo")


def fetch_issues(repo: str, token: str | None = None, state: str = "open", per_page: int = 30) -> list[dict[str, Any]]:
    owner, name = parse_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{name}/issues"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(
        url,
        headers=headers,
        params={"state": state, "per_page": per_page},
        timeout=30,
    )
    response.raise_for_status()
    issues = response.json()
    return [issue for issue in issues if "pull_request" not in issue]


def issues_to_documents(repo: str, issues: list[dict[str, Any]]) -> list[SourceDocument]:
    documents: list[SourceDocument] = []
    for issue in issues:
        number = issue["number"]
        title = issue.get("title") or f"Issue #{number}"
        body = issue.get("body") or ""
        labels = [label.get("name") for label in issue.get("labels", [])]
        text = f"Issue #{number}: {title}\n\n{body}"
        documents.append(
            SourceDocument(
                id=f"{repo}#issue-{number}",
                title=f"Issue #{number}: {title}",
                text=text,
                source_type="github_issue",
                url=issue.get("html_url"),
                metadata={
                    "repo": repo,
                    "number": number,
                    "state": issue.get("state"),
                    "labels": labels,
                    "created_at": issue.get("created_at"),
                    "updated_at": issue.get("updated_at"),
                },
            )
        )
    return documents
