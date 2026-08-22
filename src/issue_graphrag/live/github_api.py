"""Narrow GitHub REST client for data omitted from webhook payloads."""

from __future__ import annotations

from typing import Any

import requests

from issue_graphrag.ingest.github_loader import parse_repo
from issue_graphrag.live.models import RepoEvent

DEFAULT_API_VERSION = "2026-03-10"
MAX_PULL_FILE_PAGES = 30


def pull_request_number(event: RepoEvent) -> int | None:
    if event.event_type == "pull_request":
        pull = event.payload.get("pull_request") or {}
        return int(pull["number"]) if pull.get("number") is not None else None
    if event.event_type == "issue_comment":
        issue = event.payload.get("issue") or {}
        if issue.get("pull_request") is not None and issue.get("number") is not None:
            return int(issue["number"])
    return None


class GitHubClient:
    """Fetch pull request files with GitHub's documented pagination and cap."""

    def __init__(
        self,
        token: str | None = None,
        session: Any | None = None,
        timeout: int = 30,
        api_version: str = DEFAULT_API_VERSION,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.token = token
        self.session = session or requests.Session()
        self.timeout = timeout
        self.api_version = api_version

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "github-issue-graphrag",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def fetch_pull_request_files(self, repo: str, number: int) -> list[str]:
        owner, name = parse_repo(repo)
        url = f"https://api.github.com/repos/{owner}/{name}/pulls/{number}/files"
        files: set[str] = set()

        for page in range(1, MAX_PULL_FILE_PAGES + 1):
            response = self.session.get(
                url,
                headers=self._headers(),
                params={"per_page": 100, "page": page},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("GitHub pull-request files response is not a list")
            for entry in payload:
                if isinstance(entry, dict) and entry.get("filename"):
                    files.add(str(entry["filename"]))
            if len(payload) < 100:
                break

        return sorted(files)
