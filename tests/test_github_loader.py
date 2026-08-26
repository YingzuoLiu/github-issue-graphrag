from __future__ import annotations

from issue_graphrag.ingest.github_loader import (
    fetch_comments,
    fetch_issues_and_pulls,
    fetch_pull_request_files,
)

REPO = "example/project"


class FakeResponse:
    def __init__(self, payload):  # noqa: ANN001
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):  # noqa: ANN201
        return self.payload


class FakeSession:
    def __init__(self, pages):  # noqa: ANN001
        self.pages = list(pages)
        self.calls = []

    def get(self, url, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append((url, kwargs))
        return FakeResponse(self.pages.pop(0))


def test_bootstrap_comments_follow_pagination_and_explicit_caps():
    pages = [
        [{"id": index} for index in range(100)],
        [{"id": 100}],
    ]
    session = FakeSession(pages)

    comments = fetch_comments(REPO, 1, limit=101, max_pages=2, session=session)

    assert len(comments) == 101
    assert [call[1]["params"]["page"] for call in session.calls] == [1, 2]
    assert [call[1]["params"]["per_page"] for call in session.calls] == [100, 1]


def test_bootstrap_items_follow_pagination_and_stop_at_the_declared_limit():
    pages = [
        [{"number": index} for index in range(100)],
        [{"number": 100}, {"number": 101}],
    ]
    session = FakeSession(pages)

    items = fetch_issues_and_pulls(REPO, limit=101, max_pages=2, session=session)

    assert len(items) == 101
    assert [call[1]["params"]["page"] for call in session.calls] == [1, 2]
    assert [call[1]["params"]["per_page"] for call in session.calls] == [100, 1]
    assert all(call[1]["params"]["state"] == "all" for call in session.calls)


def test_bootstrap_pull_files_follow_pagination_and_are_canonicalized():
    pages = [
        [{"filename": f"src/{index}.py"} for index in range(100)],
        [{"filename": "README.md"}, {"filename": "README.md"}],
    ]
    session = FakeSession(pages)

    files = fetch_pull_request_files(REPO, 2, limit=102, max_pages=2, session=session)

    assert len(files) == 101
    assert files == sorted(set(files))
    assert [call[1]["params"]["page"] for call in session.calls] == [1, 2]
    assert [call[1]["params"]["per_page"] for call in session.calls] == [100, 2]
