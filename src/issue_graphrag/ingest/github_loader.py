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


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get(url: str, token: str | None, params: dict[str, Any] | None = None) -> Any:
    response = requests.get(url, headers=_headers(token), params=params or {}, timeout=30)
    response.raise_for_status()
    return response.json()


def _paginate(
    url: str,
    token: str | None,
    *,
    limit: int,
    max_pages: int,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch bounded GitHub list pages without silently dropping page two."""
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    collected: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        remaining = limit - len(collected)
        if remaining <= 0:
            break
        per_page = min(100, remaining)
        payload = _get(
            url,
            token,
            {**(params or {}), "per_page": per_page, "page": page},
        )
        if not isinstance(payload, list):
            raise ValueError(f"GitHub returned a non-list payload for {url}")
        rows = [row for row in payload if isinstance(row, dict)]
        collected.extend(rows[:remaining])
        if len(payload) < per_page:
            break
    return collected


def fetch_issues_and_pulls(
    repo: str,
    token: str | None = None,
    state: str = "all",
    limit: int = 30,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Fetch issues *and* pull requests.

    The batch pipeline drops pull requests because it only needs issue prose.
    The live contribution graph needs them: a pull request is what turns an open
    issue into a claimed one.
    """
    owner, name = parse_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{name}/issues"

    return _paginate(
        url,
        token,
        limit=limit,
        max_pages=max_pages,
        params={"state": state},
    )


def fetch_comments(
    repo: str,
    number: int,
    token: str | None = None,
    *,
    limit: int = 300,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    owner, name = parse_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{name}/issues/{number}/comments"
    return _paginate(url, token, limit=limit, max_pages=max_pages)


def fetch_pull_request(repo: str, number: int, token: str | None = None) -> dict[str, Any]:
    """The issues endpoint omits merged state, so pull requests need a second call."""
    owner, name = parse_repo(repo)
    return _get(f"https://api.github.com/repos/{owner}/{name}/pulls/{number}", token)


def fetch_pull_request_files(
    repo: str,
    number: int,
    token: str | None = None,
    *,
    limit: int = 3000,
    max_pages: int = 30,
) -> list[str]:
    owner, name = parse_repo(repo)
    url = f"https://api.github.com/repos/{owner}/{name}/pulls/{number}/files"
    rows = _paginate(url, token, limit=limit, max_pages=max_pages)
    return sorted({str(entry["filename"]) for entry in rows if entry.get("filename")})


def to_seed_item(
    repo: str,
    raw: dict[str, Any],
    kind: str,
    comments: list[dict[str, Any]] | None = None,
    files: list[str] | None = None,
) -> dict[str, Any]:
    """Shape one API record into the live index's seed format."""
    return {
        "kind": kind,
        "repo": repo,
        "number": raw["number"],
        "title": raw.get("title") or "",
        "body": raw.get("body") or "",
        "state": raw.get("state") or "open",
        "merged": bool(raw.get("merged")) or raw.get("merged_at") is not None,
        "draft": bool(raw.get("draft")),
        "labels": sorted(
            {str(label.get("name", "")) for label in raw.get("labels", []) if isinstance(label, dict)}
            - {""}
        ),
        "assignees": sorted(
            {
                str(assignee.get("login", ""))
                for assignee in raw.get("assignees", [])
                if isinstance(assignee, dict)
            }
            - {""}
        ),
        "author": (raw.get("user") or {}).get("login", ""),
        "url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
        "merged_at": raw.get("merged_at"),
        "files": sorted(files or []),
        "comments": {
            str(comment["id"]): {
                "id": str(comment["id"]),
                "author": (comment.get("user") or {}).get("login", ""),
                "body": comment.get("body") or "",
                "url": comment.get("html_url"),
                "created_at": comment.get("created_at"),
                "updated_at": comment.get("updated_at"),
            }
            for comment in (comments or [])
        },
    }


def build_live_seed(
    repo: str,
    token: str | None = None,
    state: str = "all",
    limit: int = 30,
    with_comments: bool = True,
    with_files: bool = True,
    item_max_pages: int = 10,
    comment_limit_per_item: int = 300,
    comment_max_pages: int = 10,
    file_limit_per_pull: int = 3000,
    file_max_pages: int = 30,
) -> dict[str, Any]:
    """Build a live-index snapshot: issues, pull requests, comments and files."""
    items: list[dict[str, Any]] = []

    for raw in fetch_issues_and_pulls(
        repo,
        token=token,
        state=state,
        limit=limit,
        max_pages=item_max_pages,
    ):
        number = raw["number"]
        is_pull = "pull_request" in raw
        comments = (
            fetch_comments(
                repo,
                number,
                token,
                limit=comment_limit_per_item,
                max_pages=comment_max_pages,
            )
            if with_comments
            else []
        )

        if is_pull:
            detail = fetch_pull_request(repo, number, token)
            files = (
                fetch_pull_request_files(
                    repo,
                    number,
                    token,
                    limit=file_limit_per_pull,
                    max_pages=file_max_pages,
                )
                if with_files
                else []
            )
            items.append(to_seed_item(repo, {**raw, **detail}, "pull_request", comments, files))
        else:
            items.append(to_seed_item(repo, raw, "issue", comments, []))

    return {"repo": repo, "items": items}
