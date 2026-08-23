from __future__ import annotations

import pytest
from conftest import REPO, issue_payload, make_event, pull_payload

from issue_graphrag.http_boundary import ReadOnlyViolation
from issue_graphrag.ingest.github_loader import to_seed_item
from issue_graphrag.live.github_api import GitHubClient, dependency_issue, pull_request_number


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


def test_dependency_issue_uses_the_blocked_issues_repository():
    event = make_event(
        "d-dependency",
        "issue_dependencies",
        {
            "action": "blocked_by_added",
            "blocked_issue": {
                **issue_payload(7),
                "repository_url": f"https://api.github.com/repos/{REPO}",
            },
            "blocking_issue": issue_payload(8),
        },
        "2024-06-01T00:00:03Z",
    )

    assert dependency_issue(event) == (REPO, 7)


def test_open_blocking_dependency_count_is_paginated_and_ignores_closed_issues():
    first_page = [{"state": "open"}] * 60 + [{"state": "closed"}] * 40
    session = FakeSession([first_page, [{"state": "open"}, {"state": "closed"}]])
    client = GitHubClient(token="token", session=session)

    count = client.fetch_open_blocking_dependency_count(REPO, 7)

    assert count == 61
    assert [call[1]["params"]["page"] for call in session.calls] == [1, 2]
    assert session.calls[0][0].endswith("/issues/7/dependencies/blocked_by")


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


def test_live_client_refuses_a_write_before_it_reaches_github():
    session = FakeSession([])
    client = GitHubClient(token="token", session=session)

    with pytest.raises(ReadOnlyViolation):
        client.session.post(
            f"https://api.github.com/repos/{REPO}/issues/1/labels",
            json={"labels": ["help wanted"]},
        )

    assert session.calls == []
    assert client.write_request_count == 1


def test_live_client_fetches_are_counted_as_reads_with_zero_writes():
    session = FakeSession([[{"filename": "a.py"}], [{"state": "open"}]])
    client = GitHubClient(token="token", session=session)

    client.fetch_pull_request_files(REPO, 950)
    client.fetch_open_blocking_dependency_count(REPO, 7)

    assert client.read_request_count == 2
    assert client.write_request_count == 0
