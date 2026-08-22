from __future__ import annotations

from conftest import REPO, issue_payload, make_event, pull_payload

from issue_graphrag.live.events import EventLog
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.indexer import NullExtractor
from issue_graphrag.models import ExtractionResult
from issue_graphrag.live.processor import DeliveryProcessor
from issue_graphrag.live.store import read_state

NOW = "2024-06-01T10:00:00Z"


class FakeGitHubClient:
    def __init__(self, files=None, error: Exception | None = None):
        self.files = files or []
        self.error = error
        self.calls = []

    def fetch_pull_request_files(self, repo: str, number: int):
        self.calls.append((repo, number))
        if self.error:
            raise self.error
        return self.files


class CountingExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, text_units):  # noqa: ANN001
        self.calls += 1
        return ExtractionResult()


class FailOnceEventLog(EventLog):
    def __init__(self, path):
        super().__init__(path)
        self.failed = False

    def append_once(self, event):  # noqa: ANN001
        if not self.failed:
            self.failed = True
            raise OSError("simulated disk interruption")
        return super().append_once(event)


def _processor(tmp_path, inbox, extractor=None, github=None, log=None, max_attempts=3):
    return DeliveryProcessor(
        repo=REPO,
        inbox=inbox,
        state_path=tmp_path / "live_state.json",
        event_log=log or EventLog(tmp_path / "events.jsonl"),
        extractor=extractor or NullExtractor(),
        github=github,
        lease_seconds=30,
        retry_delay_seconds=0,
        max_attempts=max_attempts,
    )


def test_worker_hydrates_pull_request_files_before_indexing(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "delivery-1",
        "pull_request",
        {"action": "opened", "pull_request": pull_payload(950)},
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    github = FakeGitHubClient(["src/live.py", "README.md"])

    result = _processor(tmp_path, inbox, github=github).process_one(now=NOW)

    assert result is not None
    assert result.status == "succeeded"
    assert github.calls == [(REPO, 950)]
    state = read_state(tmp_path / "live_state.json")
    assert state.items[f"{REPO}#pull-950"].files == ["README.md", "src/live.py"]
    stored = inbox.get("delivery-1")
    assert stored is not None
    assert stored.event.attachments["files"] == ["README.md", "src/live.py"]
    assert EventLog(tmp_path / "events.jsonl").delivery_ids() == {"delivery-1"}


def test_pr_comment_can_hydrate_files_for_a_pr_first_seen_through_the_comment(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    parent = {**issue_payload(951), "pull_request": {"url": "https://api.github.com/pulls/951"}}
    event = make_event(
        "delivery-1",
        "issue_comment",
        {"action": "created", "issue": parent, "comment": {"id": 1, "body": "hi"}},
        NOW,
    )
    inbox.enqueue(event, now=NOW)

    result = _processor(
        tmp_path,
        inbox,
        github=FakeGitHubClient(["packages/api/client.py"]),
    ).process_one(now=NOW)

    assert result is not None and result.status == "succeeded"
    state = read_state(tmp_path / "live_state.json")
    assert state.items[f"{REPO}#pull-951"].files == ["packages/api/client.py"]


def test_later_pr_comment_reuses_known_files_instead_of_refetching(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    github = FakeGitHubClient(["src/live.py"])
    opened = make_event(
        "delivery-1",
        "pull_request",
        {"action": "opened", "pull_request": pull_payload(950)},
        NOW,
    )
    inbox.enqueue(opened, now=NOW)
    processor = _processor(tmp_path, inbox, github=github)
    assert processor.process_one(now=NOW).status == "succeeded"  # type: ignore[union-attr]

    parent = {**issue_payload(950), "pull_request": {"url": "https://api.github.com/pulls/950"}}
    comment = make_event(
        "delivery-2",
        "issue_comment",
        {"action": "created", "issue": parent, "comment": {"id": 1, "body": "hi"}},
        "2024-06-01T10:00:01Z",
    )
    inbox.enqueue(comment, now="2024-06-01T10:00:01Z")
    assert processor.process_one(now="2024-06-01T10:00:01Z").status == "succeeded"  # type: ignore[union-attr]

    assert github.calls == [(REPO, 950)]


def test_github_only_mode_leaves_changed_text_pending_for_a_future_extractor(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "delivery-1",
        "issues",
        {"action": "opened", "issue": issue_payload(7, body="extract me later")},
        NOW,
    )
    inbox.enqueue(event, now=NOW)

    result = _processor(tmp_path, inbox).process_one(now=NOW)

    assert result is not None and result.status == "succeeded"
    state = read_state(tmp_path / "live_state.json")
    assert f"{REPO}#issue-7" not in state.extraction_signatures


def test_crash_after_state_write_recovers_without_duplicate_extraction_or_log(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "delivery-1",
        "issues",
        {"action": "opened", "issue": issue_payload(7, body="new text")},
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    extractor = CountingExtractor()
    log = FailOnceEventLog(tmp_path / "events.jsonl")
    processor = _processor(tmp_path, inbox, extractor=extractor, log=log)

    first = processor.process_one(now=NOW)
    assert first is not None and first.status == "retrying"
    assert read_state(tmp_path / "live_state.json").has_delivery("delivery-1")
    indexed_at = inbox.get("delivery-1").event.indexed_at  # type: ignore[union-attr]

    second = processor.process_one(now=NOW)
    assert second is not None and second.status == "succeeded"
    assert extractor.calls == 1
    assert EventLog(tmp_path / "events.jsonl").delivery_ids() == {"delivery-1"}
    assert EventLog(tmp_path / "events.jsonl").read_all()[0].indexed_at == indexed_at


def test_github_fetch_failure_is_retried_without_writing_state(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "delivery-1",
        "pull_request",
        {"action": "opened", "pull_request": pull_payload(950)},
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    processor = _processor(
        tmp_path,
        inbox,
        github=FakeGitHubClient(error=RuntimeError("GitHub unavailable")),
        max_attempts=2,
    )

    first = processor.process_one(now=NOW)
    second = processor.process_one(now=NOW)

    assert first is not None and first.status == "retrying"
    assert second is not None and second.status == "failed"
    assert not (tmp_path / "live_state.json").exists()
    assert inbox.get("delivery-1").attempts == 2  # type: ignore[union-attr]
