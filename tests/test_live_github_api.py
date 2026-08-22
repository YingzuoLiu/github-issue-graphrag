from __future__ import annotations

from conftest import REPO, issue_payload, make_event, pull_payload

from issue_graphrag.ingest.github_loader import to_seed_item
from issue_graphrag.live.github_api import GitHubClient, pull_request_number


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.pages.pop(0))


def test_pull_request_files_are_paginated_and_canonicalized():
    first_page = [{"filename": f"src/{index}.py"} for index in range(100)]
    session = FakeSession([first_page, [{"filename": "README.md"}, {"filename": "README.md"}]])
    client = GitHubClient(token="token", session=session)

    files = client.fetch_pull_request_files(REPO, 950)

    assert len(files) == 101
    assert files == sorted(set(files))
    assert [call[1]["params"]["page"] for call in session.calls] == [1, 2]
    assert session.calls[0][1]["params"]["per_page"] == 100
    assert session.calls[0][1]["headers"]["Authorization"] == "Bearer token"


def test_pull_request_number_understands_both_pr_and_comment_payloads():
    pull = make_event(
        "d-1",
        "pull_request",
        {"action": "opened", "pull_request": pull_payload(950)},
        "2024-06-01T00:00:00Z",
    )
    parent = {**issue_payload(951), "pull_request": {"url": "https://api.github.com/pulls/951"}}
    comment = make_event(
        "d-2",
        "issue_comment",
        {"action": "created", "issue": parent, "comment": {"id": 1, "body": "hi"}},
        "2024-06-01T00:00:01Z",
    )
    issue = make_event(
        "d-3",
        "issue_comment",
        {"action": "created", "issue": issue_payload(952), "comment": {"id": 2}},
        "2024-06-01T00:00:02Z",
    )

    assert pull_request_number(pull) == 950
    assert pull_request_number(comment) == 951
    assert pull_request_number(issue) is None


def test_snapshot_loader_preserves_sorted_unique_assignees():
    raw = issue_payload(
        1,
        assignees=[
            {"login": "octocat"},
            {"login": "hubot"},
            {"login": "octocat"},
            {"login": ""},
        ],
    )

    seed = to_seed_item(REPO, raw, "issue")

    assert seed["assignees"] == ["hubot", "octocat"]
