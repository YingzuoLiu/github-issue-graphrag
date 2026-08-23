from __future__ import annotations

import pytest
from conftest import REPO, issue_payload, make_event, pull_payload

from issue_graphrag.live.events import EventLog
from issue_graphrag.live.extraction import LLMExtractor
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.indexer import NullExtractor
from issue_graphrag.live.models import LiveState, RepoItem
from issue_graphrag.live.processor import DeliveryProcessor, ProcessingResult, run_worker_loop
from issue_graphrag.live.repositories import read_freshness
from issue_graphrag.live.semantic_operations import (
    BatchPolicy,
    ExtractionCache,
    QuotaLedger,
    SemanticBatchRunner,
)
from issue_graphrag.live.store import read_state, write_state
from issue_graphrag.llm.client import CompletionMetadata, StructuredCompletion
from issue_graphrag.models import ExtractionResult

NOW = "2024-06-01T10:00:00Z"


class FakeGitHubClient:
    def __init__(
        self,
        files=None,
        error: Exception | None = None,
        dependency_counts=None,
        file_answers=None,
    ):
        self.files = files or []
        self.error = error
        self.calls = []
        self.dependency_counts = list(dependency_counts or [0])
        self.dependency_calls = []
        # Successive answers, so a test can make GitHub's reply drift between a
        # first attempt and its retry.
        self.file_answers = list(file_answers) if file_answers is not None else None

    def fetch_pull_request_files(self, repo: str, number: int):
        self.calls.append((repo, number))
        if self.error:
            raise self.error
        if self.file_answers is not None:
            return self.file_answers.pop(0)
        return self.files

    def fetch_open_blocking_dependency_count(self, repo: str, number: int):
        self.dependency_calls.append((repo, number))
        if self.error:
            raise self.error
        return self.dependency_counts.pop(0)


class CountingExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, text_units):  # noqa: ANN001
        self.calls += 1
        return ExtractionResult()


class FailingExtractor:
    def __init__(self):
        self.calls = 0

    def extract(self, text_units):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("semantic provider unavailable")


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
        freshness_path=tmp_path / "freshness.json",
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
    freshness = read_freshness(tmp_path / "freshness.json", REPO)
    assert freshness.semantic_status == "pending"


def test_source_delivery_commits_before_semantic_work_runs(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "delivery-1",
        "issues",
        {"action": "opened", "issue": issue_payload(7, body="extract later")},
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    extractor = FailingExtractor()
    processor = _processor(tmp_path, inbox, extractor=extractor)

    source = processor.process_one(now=NOW)

    assert source is not None and source.status == "succeeded"
    assert source.work_type == "delivery"
    assert extractor.calls == 0
    assert inbox.get("delivery-1").status == "succeeded"  # type: ignore[union-attr]
    assert EventLog(tmp_path / "events.jsonl").delivery_ids() == {"delivery-1"}
    state = read_state(tmp_path / "live_state.json")
    assert state.has_delivery("delivery-1")
    assert any(fact.origin == "github" for fact in state.valid_facts())

    semantic = processor.process_one(now=NOW)

    assert semantic is not None and semantic.status == "deferred"
    assert semantic.work_type == "semantic"
    assert extractor.calls == 1
    assert inbox.get("delivery-1").status == "succeeded"  # type: ignore[union-attr]
    degraded = read_freshness(tmp_path / "freshness.json", REPO)
    assert degraded.semantic_status == "degraded"
    assert degraded.last_error and "provider unavailable" in degraded.last_error

    # Platform observations continue while the provider is down, but they do
    # not mislabel the still-unresolved semantic incident as merely pending.
    later = "2024-06-01T10:00:01Z"
    inbox.enqueue(
        make_event(
            "delivery-2",
            "issues",
            {"action": "opened", "issue": issue_payload(8, body="another issue")},
            later,
        ),
        now=later,
    )
    continued = processor.process_one(now=later)
    assert continued is not None and continued.work_type == "delivery"
    still_degraded = read_freshness(tmp_path / "freshness.json", REPO)
    assert still_degraded.semantic_status == "degraded"
    assert still_degraded.last_error == degraded.last_error


def test_idle_worker_materializes_bootstrap_pending_state_into_durable_work(tmp_path):
    item = RepoItem(
        kind="issue",
        repo=REPO,
        number=7,
        title="Issue 7",
        body="bootstrap semantics",
        effective_at=NOW,
        source_delivery_id="seed",
    )
    state = LiveState(repo=REPO, items={item.document_id: item}, last_event_at=NOW)
    write_state(tmp_path / "live_state.json", state)
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    extractor = CountingExtractor()

    result = _processor(tmp_path, inbox, extractor=extractor).process_one(now=NOW)

    assert result is not None and result.status == "succeeded"
    assert result.work_type == "semantic"
    assert extractor.calls == 1
    restored = read_state(tmp_path / "live_state.json")
    assert restored.extraction_signatures[item.document_id] == item.extraction_signature()
    assert inbox.count_semantic_jobs() == 0


def test_deferred_semantics_preserve_last_good_facts_and_resume_after_restart(
    tmp_path,
    extractor,
):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    opened = make_event(
        "delivery-1",
        "issues",
        {"action": "opened", "issue": issue_payload(7, body="RRF is proposed")},
        NOW,
    )
    inbox.enqueue(opened, now=NOW)
    initial = _processor(tmp_path, inbox, extractor=extractor)
    assert initial.process_one(now=NOW).status == "succeeded"  # type: ignore[union-attr]
    assert initial.process_one(now=NOW).status == "succeeded"  # type: ignore[union-attr]

    before = read_state(tmp_path / "live_state.json")
    document_id = f"{REPO}#issue-7"
    old_signature = before.extraction_signatures[document_id]
    old_facts = [fact.model_dump() for fact in before.document_facts(document_id, "llm")]
    assert old_facts

    edited_at = "2024-06-01T10:00:01Z"
    edited = make_event(
        "delivery-2",
        "issues",
        {
            "action": "edited",
            "issue": issue_payload(
                7,
                title="Issue 7 updated",
                body="RRF is proposed with more context",
                updated_at=edited_at,
            ),
        },
        edited_at,
    )
    inbox.enqueue(edited, now=edited_at)
    failing_extractor = FailingExtractor()
    failing = _processor(tmp_path, inbox, extractor=failing_extractor)

    source = failing.process_one(now=edited_at)
    deferred = failing.process_one(now=edited_at)

    assert source is not None and source.status == "succeeded"
    assert deferred is not None and deferred.status == "deferred"
    after_failure = read_state(tmp_path / "live_state.json")
    assert after_failure.items[document_id].title == "Issue 7 updated"
    assert after_failure.extraction_signatures[document_id] == old_signature
    assert [fact.model_dump() for fact in after_failure.document_facts(document_id, "llm")] == old_facts
    assert inbox.get("delivery-2").status == "succeeded"  # type: ignore[union-attr]

    resumed = _processor(tmp_path, DeliveryInbox(tmp_path / "inbox.sqlite"), extractor=extractor)
    completed = resumed.process_one(now=edited_at)

    assert completed is not None and completed.status == "succeeded"
    assert completed.work_type == "semantic"
    restored = read_state(tmp_path / "live_state.json")
    assert restored.extraction_signatures[document_id] == restored.items[
        document_id
    ].extraction_signature()
    assert restored.extraction_signatures[document_id] != old_signature
    assert DeliveryInbox(tmp_path / "inbox.sqlite").count_semantic_jobs() == 0


def test_dependency_webhooks_hydrate_active_count_and_clear_blocked_status(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    blocked_issue = {
        **issue_payload(7),
        "repository_url": f"https://api.github.com/repos/{REPO}",
    }
    github = FakeGitHubClient(dependency_counts=[2, 0])
    processor = _processor(tmp_path, inbox, github=github)

    added = make_event(
        "dependency-added",
        "issue_dependencies",
        {
            "action": "blocked_by_added",
            "blocked_issue": blocked_issue,
            "blocking_issue": issue_payload(8),
        },
        NOW,
    )
    inbox.enqueue(added, now=NOW)
    first = processor.process_one(now=NOW)

    assert first is not None and first.status == "succeeded"
    state = read_state(tmp_path / "live_state.json")
    item = state.items[f"{REPO}#issue-7"]
    assert item.blocking_dependency_count == 2
    assert any(
        fact.predicate == "has_blocking_dependencies" for fact in state.valid_facts()
    )
    stored = inbox.get("dependency-added")
    assert stored is not None
    assert stored.event.attachments["blocking_dependency_count"] == 2

    removed_at = "2024-06-01T10:00:01Z"
    removed = make_event(
        "dependency-removed",
        "issue_dependencies",
        {
            "action": "blocked_by_removed",
            "blocked_issue": blocked_issue,
            "blocking_issue": issue_payload(8),
        },
        removed_at,
    )
    inbox.enqueue(removed, now=removed_at)
    second = processor.process_one(now=removed_at)

    assert second is not None and second.status == "succeeded"
    state = read_state(tmp_path / "live_state.json")
    assert state.items[f"{REPO}#issue-7"].blocking_dependency_count == 0
    assert not any(
        fact.predicate == "has_blocking_dependencies" for fact in state.valid_facts()
    )
    assert github.dependency_calls == [(REPO, 7), (REPO, 7)]


def test_dependency_retry_reuses_the_observation_already_committed_to_state(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "dependency-crash",
        "issue_dependencies",
        {
            "action": "blocked_by_added",
            "blocked_issue": {
                **issue_payload(7),
                "repository_url": f"https://api.github.com/repos/{REPO}",
            },
            "blocking_issue": issue_payload(8),
        },
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    github = FakeGitHubClient(dependency_counts=[2, 0])
    log = FailOnceEventLog(tmp_path / "events.jsonl")
    processor = _processor(tmp_path, inbox, github=github, log=log)

    first = processor.process_one(now=NOW)
    second = processor.process_one(now="2024-06-01T10:00:01Z")

    assert first is not None and first.status == "retrying"
    assert second is not None and second.status == "succeeded"
    assert github.dependency_calls == [(REPO, 7)]
    state = read_state(tmp_path / "live_state.json")
    assert state.items[f"{REPO}#issue-7"].blocking_dependency_count == 2
    logged = EventLog(tmp_path / "events.jsonl").read_all()
    assert logged[0].attachments["blocking_dependency_count"] == 2


@pytest.mark.parametrize("action", ["opened", "synchronize"])
def test_pull_file_retry_reuses_the_observation_already_committed_to_state(tmp_path, action):
    """The same rule the dependency hydration follows, on the older PR-file path.

    Both actions re-read the file list on a fresh delivery, so both could
    overwrite a durable attachment whose observation is already in the state.
    """
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = make_event(
        "pull-crash",
        "pull_request",
        {"action": action, "pull_request": pull_payload(950)},
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    github = FakeGitHubClient(file_answers=[["a.py"], ["a.py", "b.py"]])
    log = FailOnceEventLog(tmp_path / "events.jsonl")
    processor = _processor(tmp_path, inbox, github=github, log=log)

    first = processor.process_one(now=NOW)
    second = processor.process_one(now="2024-06-01T10:00:01Z")

    assert first is not None and first.status == "retrying"
    assert second is not None and second.status == "succeeded"
    assert github.calls == [(REPO, 950)]
    state = read_state(tmp_path / "live_state.json")
    logged = EventLog(tmp_path / "events.jsonl").read_all()
    assert state.items[f"{REPO}#pull-950"].files == ["a.py"]
    assert logged[0].attachments["files"] == ["a.py"]


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
    third = processor.process_one(now=NOW)
    assert third is not None and third.status == "succeeded"
    assert third.work_type == "semantic"
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


def test_worker_loop_survives_an_iteration_failure_and_keeps_processing():
    class FlakyProcessor:
        def __init__(self):
            self.calls = 0

        def process_one(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary sqlite failure")
            if self.calls == 2:
                return ProcessingResult("delivery-2", "retrying", error="retry me")
            raise KeyboardInterrupt

    processor = FlakyProcessor()
    results = []
    errors = []
    sleeps = []

    with pytest.raises(KeyboardInterrupt):
        run_worker_loop(
            processor,
            poll_seconds=0,
            on_result=results.append,
            on_error=errors.append,
            sleep=sleeps.append,
        )

    assert processor.calls == 3
    assert [result.delivery_id for result in results] == ["delivery-2"]
    assert [str(error) for error in errors] == ["temporary sqlite failure"]
    assert sleeps == [1.0]


def test_partial_operational_batches_publish_nothing_until_document_is_complete(tmp_path):
    class StructuredClient:
        model = "google/gemini-3.1-flash-lite"

        def __init__(self):
            self.calls = 0

        def complete_structured(self, prompt, **kwargs):  # noqa: ANN001
            self.calls += 1
            return StructuredCompletion(
                content=(
                    '{"entities":[{"name":"Graph RAG","type":"FEATURE",'
                    '"description":"mentioned in this unit"}],"relationships":[]}'
                ),
                metadata=CompletionMetadata(
                    self.model,
                    self.model,
                    "Google",
                    f"gen-{self.calls}",
                    100,
                    20,
                    0.0001,
                ),
            )

    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    body = "Graph RAG needs durable extraction. " * 180
    event = make_event(
        "delivery-long",
        "issues",
        {"action": "opened", "issue": issue_payload(7, body=body)},
        NOW,
    )
    inbox.enqueue(event, now=NOW)
    client = StructuredClient()
    extractor = LLMExtractor(client)
    batch_runner = SemanticBatchRunner(
        repo=REPO,
        extractor=extractor,
        cache=ExtractionCache(tmp_path / "extraction_cache.sqlite"),
        quota=QuotaLedger(tmp_path / "llm_operations.sqlite"),
        batch_policy=BatchPolicy(
            max_calls=1,
            max_input_tokens=100_000,
            max_output_tokens=10_000,
            max_output_tokens_per_call=800,
        ),
    )
    processor = DeliveryProcessor(
        repo=REPO,
        inbox=inbox,
        state_path=tmp_path / "live_state.json",
        event_log=EventLog(tmp_path / "events.jsonl"),
        extractor=extractor,
        lease_seconds=30,
        retry_delay_seconds=0,
        freshness_path=tmp_path / "freshness.json",
        semantic_runner=batch_runner,
    )

    source = processor.process_one(now=NOW)
    partial = processor.process_one(now=NOW)

    assert source is not None and source.work_type == "delivery" and source.status == "succeeded"
    assert partial is not None and partial.work_type == "semantic" and partial.status == "deferred"
    state = read_state(tmp_path / "live_state.json")
    document_id = f"{REPO}#issue-7"
    assert document_id not in state.extraction_signatures
    assert not [fact for fact in state.facts if fact.origin == "llm"]
    job = inbox.get_semantic_job(document_id)
    assert job is not None and job.next_unit_index == 1

    while inbox.count_semantic_jobs():
        processor.process_one(now=NOW)

    completed = read_state(tmp_path / "live_state.json")
    assert completed.extraction_signatures[document_id] == completed.items[
        document_id
    ].extraction_signature()
    assert [fact for fact in completed.facts if fact.origin == "llm"]
    assert client.calls > 1
