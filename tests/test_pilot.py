from __future__ import annotations

from typing import Any

from issue_graphrag.pilot import (
    GitHubPilotClient,
    evaluate_snapshot,
    make_snapshot,
    render_markdown,
)

REPO = "example/project"
NOW = "2026-08-22T10:00:00Z"


def _issue(
    number: int,
    *,
    title: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title or f"Issue {number}",
        "body": "",
        "state": "open",
        "labels": [{"name": label} for label in labels or []],
        "assignees": [{"login": login} for login in assignees or []],
        "locked": locked,
        "issue_dependencies_summary": {
            "blocked_by": 0,
            "blocking": 0,
            "total_blocked_by": 0,
            "total_blocking": 0,
        },
        "user": {"login": "author"},
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "created_at": "2026-08-01T00:00:00Z",
        "updated_at": f"2026-08-{20 - number:02d}T00:00:00Z",
        "closed_at": None,
        "comments": 0,
    }


def _pull(number: int, body: str) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": body,
        "state": "open",
        "draft": False,
        "pull_request": {"url": f"https://api.github.com/repos/{REPO}/pulls/{number}"},
        "labels": [],
        "assignees": [],
        "locked": False,
        "user": {"login": "contributor"},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": "2026-08-10T00:00:00Z",
        "updated_at": "2026-08-21T00:00:00Z",
        "closed_at": None,
        "comments": 0,
    }


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, headers=None):  # noqa: ANN001
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):  # noqa: ANN201
        return self.payload


class FakeSession:
    def __init__(self, payloads):  # noqa: ANN001
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append((url, kwargs))
        return FakeResponse(self.payloads.pop(0))


def test_pilot_client_uses_only_gets_and_groups_recent_comments():
    issue = _issue(1)
    pull = _pull(2, "Fixes #1")
    comment = {
        "id": 99,
        "issue_url": f"https://api.github.com/repos/{REPO}/issues/1",
        "body": "I can reproduce this.",
        "user": {"login": "reader"},
        "html_url": f"https://github.com/{REPO}/issues/1#issuecomment-99",
        "created_at": "2026-08-21T01:00:00Z",
        "updated_at": "2026-08-21T01:00:00Z",
    }
    session = FakeSession([[issue, pull], [comment]])
    client = GitHubPilotClient(session=session)

    snapshot = client.fetch_snapshot(
        REPO,
        issue_limit=1,
        pull_limit=1,
        max_pages=1,
        comment_limit=100,
        fetched_at=NOW,
    )

    assert snapshot.request_count == 2
    assert len(session.calls) == 2
    assert all(call[1]["headers"]["User-Agent"] for call in session.calls)
    assert snapshot.seed["items"][0]["comments"]["99"]["body"] == "I can reproduce this."
    assert len(snapshot.fingerprint) == 64


def test_pilot_models_assignees_while_separating_ambiguous_pr_references():
    assigned = _issue(1, assignees=["maintainer"])
    newcomer = _issue(2, labels=["good first issue"])
    closing_target = _issue(3)
    weak_target = _issue(5)
    snapshot = make_snapshot(
        REPO,
        [
            assigned,
            newcomer,
            closing_target,
            weak_target,
            _pull(4, "Fixes #3"),
            _pull(6, "Related discussion: #5"),
        ],
        [],
        fetched_at=NOW,
        request_count=2,
    )

    result = evaluate_snapshot(snapshot, top_k=10, false_available_threshold=0.05)

    assert result["system_status_counts"] == {
        "available": 1,
        "claimed": 3,
        "blocked": 0,
    }
    assert result["metrics"]["false_available_count"] == 0
    assert result["metrics"]["false_available_rate"] == 0.0
    assert result["false_available_examples"] == []
    assert result["metrics"]["causal_evidence_url_coverage"] == 1.0
    assert result["metrics"]["without_assignee_fact_ablation"]["false_available_rate"] == 0.5
    assert result["metrics"]["oracle_actionable_coverage"] == 0.5
    assert result["metrics"]["oracle_actionable_not_returned_count"] == 1
    assert result["oracle_actionable_not_returned_examples"][0]["number"] == 5
    assert result["metrics"]["ambiguous_plain_reference_claims"] == 1
    assert result["ambiguous_claim_examples"][0]["number"] == 5
    assert result["precommitted_checks"]["false_available_rate_pass"]
    assert result["precommitted_checks"]["github_write_requests_are_zero"]


def test_pilot_markdown_states_what_the_dry_run_does_not_prove():
    snapshot = make_snapshot(
        REPO,
        [_issue(2, labels=["help wanted"])],
        [],
        fetched_at=NOW,
        request_count=1,
    )
    report = render_markdown([evaluate_snapshot(snapshot)], top_k=10)

    assert "not a user study" in report
    assert "**0 writes**" in report
    assert "Assignee-fact ablation on this exact snapshot" in report
    assert "Time-to-selection needs a timed A/B task" in report
    assert "Maintainer burden needs maintainer feedback" in report


def test_pilot_distinguishes_local_and_cross_repo_qualified_closing_references():
    local = make_snapshot(
        REPO,
        [_issue(1), _pull(2, f"Fixes {REPO}#1")],
        [],
        fetched_at=NOW,
        request_count=1,
    )
    cross_repo = make_snapshot(
        REPO,
        [_issue(1), _pull(2, "Fixes other/project#1")],
        [],
        fetched_at=NOW,
        request_count=1,
    )

    local_result = evaluate_snapshot(local)
    cross_repo_result = evaluate_snapshot(cross_repo)

    assert local_result["oracle"]["claimed_by_closing_pr"] == 1
    assert local_result["system_status_counts"]["claimed"] == 1
    assert local_result["metrics"]["false_available_count"] == 0
    assert cross_repo_result["oracle"]["claimed_by_closing_pr"] == 0
    assert cross_repo_result["system_status_counts"]["available"] == 1
