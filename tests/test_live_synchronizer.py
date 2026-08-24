from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from issue_graphrag.live.events import EventLog
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.indexer import NullExtractor
from issue_graphrag.live.models import RepoEvent
from issue_graphrag.live.processor import DeliveryProcessor
from issue_graphrag.live.projection import project_graph
from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.repositories import (
    RepoRegistry,
    read_freshness,
    repo_paths,
)
from issue_graphrag.live.store import read_state
from issue_graphrag.live.synchronizer import (
    CachedResponse,
    ConditionalGitHubClient,
    RateLimitedError,
    RepoSyncState,
    RepositoryObservation,
    ScheduledSynchronizer,
    SyncConfig,
    SyncResource,
    plan_reconciliation,
    read_sync_state,
    reconciliation_delivery_id,
    write_sync_state,
)

REPO = "getzep/graphiti"
NOW = "2026-08-24T02:00:00Z"
LATER = "2026-08-24T02:15:00Z"


def _issue_resource(
    *,
    title: str = "Good first issue",
    updated_at: str = NOW,
    number: int = 7,
) -> SyncResource:
    return SyncResource.observed(
        kind="issue",
        identity=f"issue:{number}",
        source_updated_at=updated_at,
        payload={
            "number": number,
            "title": title,
            "body": "Small deterministic fix.",
            "state": "open",
            "labels": [{"name": "help wanted"}],
            "assignees": [],
            "locked": False,
            "user": {"login": "maintainer"},
            "html_url": f"https://github.com/{REPO}/issues/{number}",
            "created_at": NOW,
            "updated_at": updated_at,
            "closed_at": None,
        },
        parent_kind="issue",
        parent_number=number,
    )


def _comment_resource(
    *,
    body: str = "I can reproduce this.",
    updated_at: str = NOW,
    comment_id: str = "91",
    parent_number: int = 7,
) -> SyncResource:
    return SyncResource.observed(
        kind="comment",
        identity=f"comment:{comment_id}",
        source_updated_at=updated_at,
        payload={
            "id": comment_id,
            "body": body,
            "user": {"login": "contributor"},
            "html_url": f"https://github.com/{REPO}/issues/{parent_number}#issuecomment-{comment_id}",
            "created_at": NOW,
            "updated_at": updated_at,
        },
        parent_kind="issue",
        parent_number=parent_number,
    )


def _dependency_resource(count: int, *, updated_at: str = NOW) -> SyncResource:
    return SyncResource.observed(
        kind="dependency",
        identity="dependency:7",
        source_updated_at=updated_at,
        payload={"count": count},
        attachments={"blocking_dependency_count": count},
        parent_kind="issue",
        parent_number=7,
    )


def _observation(
    resources: list[SyncResource],
    *,
    cache: dict[str, CachedResponse] | None = None,
    reads: int = 1,
    not_modified: int = 0,
    poll_interval: int = 0,
) -> RepositoryObservation:
    return RepositoryObservation(
        resources={resource.identity: resource for resource in resources},
        complete_comment_parents=frozenset(
            resource.identity
            for resource in resources
            if resource.kind in {"issue", "pull_request"}
        ),
        request_cache=cache or {},
        read_requests=reads,
        write_requests=0,
        not_modified_requests=not_modified,
        minimum_poll_interval_seconds=poll_interval,
    )


class StaticObserver:
    def __init__(self, observation: RepositoryObservation):
        self.observation = observation
        self.previous_states: list[RepoSyncState] = []

    def observe(self, repo, previous, config, observed_at):  # noqa: ANN001, ANN201
        self.previous_states.append(previous.model_copy(deep=True))
        return self.observation


class RaisingObserver:
    def __init__(self, error: Exception):
        self.error = error

    def observe(self, repo, previous, config, observed_at):  # noqa: ANN001, ANN201
        raise self.error


def _synchronizer(tmp_path, observer, *, repo: str = REPO, inbox=None, config=None):  # noqa: ANN001
    paths = RepoRegistry(tmp_path).register(repo)
    return (
        ScheduledSynchronizer(
            repo=paths.repo,
            inbox=inbox or DeliveryInbox(paths.inbox),
            sync_state_path=paths.sync_state,
            freshness_path=paths.freshness,
            observer=observer,
            config=config,
        ),
        paths,
    )


def test_repeated_snapshot_is_a_noop_and_keeps_one_durable_delivery(tmp_path):
    observer = StaticObserver(_observation([_issue_resource()]))
    synchronizer, paths = _synchronizer(tmp_path, observer)

    first = synchronizer.sync_once(now=NOW)
    second = synchronizer.sync_once(now=LATER)

    assert first.status == "succeeded" and first.enqueued == 1
    assert second.status == "succeeded" and second.planned_deliveries == 0
    assert DeliveryInbox(paths.inbox).count() == 1
    assert observer.previous_states[0].resources == {}
    assert set(observer.previous_states[1].resources) == {"issue:7"}


def test_one_changed_resource_produces_one_stable_idempotent_delivery():
    before = RepoSyncState(repo=REPO, resources={"issue:7": _issue_resource()})
    changed = _issue_resource(title="Updated title", updated_at=LATER)
    observation = _observation([changed])

    first = plan_reconciliation(REPO, before, observation)
    second = plan_reconciliation(REPO, before, observation)

    assert len(first.events) == 1
    assert first.events[0] == second.events[0]
    assert first.events[0].source == "reconciliation"
    assert first.events[0].observation_label() == "Observed during scheduled sync"
    assert first.events[0].delivery_id == reconciliation_delivery_id(
        REPO,
        changed.identity,
        changed.source_updated_at,
        changed.fingerprint,
    )


def test_comment_disappearance_in_an_observed_parent_generates_one_deletion():
    issue = _issue_resource(updated_at=LATER)
    comment = _comment_resource()
    previous = RepoSyncState(
        repo=REPO,
        resources={issue.identity: issue, comment.identity: comment},
    )

    plan = plan_reconciliation(REPO, previous, _observation([issue]))

    assert len(plan.events) == 1
    event = plan.events[0]
    assert (event.event_type, event.action, event.source) == (
        "issue_comment",
        "deleted",
        "reconciliation",
    )
    assert "comment:91" not in plan.resources


def test_reconciliation_delivery_runs_through_existing_worker_and_ranking(tmp_path):
    observer = StaticObserver(_observation([_issue_resource()]))
    synchronizer, paths = _synchronizer(tmp_path, observer)
    assert synchronizer.sync_once(now=NOW).status == "succeeded"

    processor = DeliveryProcessor(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        state_path=paths.state,
        event_log=EventLog(paths.event_log),
        extractor=NullExtractor(),
        freshness_path=paths.freshness,
    )
    result = processor.process_one(now=NOW)

    assert result is not None and result.status == "succeeded"
    state = read_state(paths.state)
    assert state.items[f"{REPO}#issue-7"].title == "Good first issue"
    ranked = opportunities(project_graph(state))
    assert [(item.number, item.status) for item in ranked] == [(7, "available")]
    stored = EventLog(paths.event_log).read_all()
    assert len(stored) == 1 and stored[0].source == "reconciliation"


def test_dependency_snapshot_uses_persisted_observation_in_existing_worker(tmp_path):
    issue = _issue_resource()
    dependency_before = _dependency_resource(0)
    dependency_after = _dependency_resource(2, updated_at=LATER)
    paths = RepoRegistry(tmp_path).register(REPO)
    write_sync_state(
        paths.sync_state,
        RepoSyncState(
            repo=REPO,
            resources={
                issue.identity: issue,
                dependency_before.identity: dependency_before,
            },
        ),
    )
    observer = StaticObserver(_observation([issue, dependency_after]))
    synchronizer = ScheduledSynchronizer(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        sync_state_path=paths.sync_state,
        freshness_path=paths.freshness,
        observer=observer,
    )

    result = synchronizer.sync_once(now=LATER)
    stored_delivery = DeliveryInbox(paths.inbox).get(
        reconciliation_delivery_id(
            REPO,
            dependency_after.identity,
            dependency_after.source_updated_at,
            dependency_after.fingerprint,
        )
    )

    assert result.planned_deliveries == 1
    assert stored_delivery is not None
    assert stored_delivery.event.attachments["blocking_dependency_count"] == 2
    processor = DeliveryProcessor(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        state_path=paths.state,
        event_log=EventLog(paths.event_log),
        extractor=NullExtractor(),
        freshness_path=paths.freshness,
    )
    processed = processor.process_one(now=LATER)
    assert processed is not None and processed.status == "succeeded"
    state = read_state(paths.state)
    assert state.items[f"{REPO}#issue-7"].blocking_dependency_count == 2
    assert opportunities(project_graph(state))[0].status == "blocked"


def test_truncated_comment_window_never_infers_a_deletion():
    issue = _issue_resource(updated_at=LATER)
    comment = _comment_resource()
    previous = RepoSyncState(
        repo=REPO,
        resources={issue.identity: issue, comment.identity: comment},
    )
    observation = RepositoryObservation(
        resources={issue.identity: issue},
        complete_comment_parents=frozenset(),
        request_cache={},
        read_requests=1,
        write_requests=0,
        not_modified_requests=0,
        minimum_poll_interval_seconds=0,
    )

    plan = plan_reconciliation(REPO, previous, observation)

    assert plan.events == ()
    assert "comment:91" in plan.resources


class FailOnSecondEnqueue:
    def __init__(self, inbox: DeliveryInbox):
        self.inbox = inbox
        self.calls = 0

    def enqueue(self, event, now):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 2:
            raise OSError("simulated checkpoint crash")
        return self.inbox.enqueue(event, now)


def test_partial_enqueue_does_not_advance_checkpoint_and_retry_converges(tmp_path):
    resources = [_issue_resource(), _comment_resource()]
    observer = StaticObserver(_observation(resources))
    paths = RepoRegistry(tmp_path).register(REPO)
    durable_inbox = DeliveryInbox(paths.inbox)
    failing = FailOnSecondEnqueue(durable_inbox)
    first = ScheduledSynchronizer(
        repo=paths.repo,
        inbox=failing,
        sync_state_path=paths.sync_state,
        freshness_path=paths.freshness,
        observer=observer,
    )

    failed = first.sync_once(now=NOW)

    assert failed.status == "failed"
    assert durable_inbox.count() == 1
    assert not paths.sync_state.exists()

    retry = ScheduledSynchronizer(
        repo=paths.repo,
        inbox=durable_inbox,
        sync_state_path=paths.sync_state,
        freshness_path=paths.freshness,
        observer=observer,
    ).sync_once(now=LATER)

    assert retry.status == "succeeded"
    assert retry.enqueued == 1 and retry.duplicates == 1
    assert durable_inbox.count() == 2
    assert set(read_sync_state(paths.sync_state, REPO).resources) == {
        "issue:7",
        "comment:91",
    }


def test_rate_limit_preserves_last_good_checkpoint_and_marks_source_stale(tmp_path):
    paths = RepoRegistry(tmp_path).register(REPO)
    last_good = RepoSyncState(repo=REPO, last_observed_at=NOW)
    write_sync_state(paths.sync_state, last_good)
    retry_at = "2026-08-24T03:00:00Z"
    synchronizer = ScheduledSynchronizer(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        sync_state_path=paths.sync_state,
        freshness_path=paths.freshness,
        observer=RaisingObserver(RateLimitedError("primary limit", retry_at)),
    )

    result = synchronizer.sync_once(now=LATER)

    assert result.status == "rate_limited" and result.next_sync_at == retry_at
    assert read_sync_state(paths.sync_state, REPO) == last_good
    freshness = read_freshness(paths.freshness, REPO)
    assert freshness.source_status == "stale"
    assert freshness.next_source_sync_at == retry_at
    assert "RateLimitedError" in (freshness.source_error or "")


def test_exhausted_network_failure_preserves_last_good_and_retries_later(tmp_path):
    paths = RepoRegistry(tmp_path).register(REPO)
    last_good = RepoSyncState(
        repo=REPO,
        last_observed_at=NOW,
        resources={"issue:7": _issue_resource()},
    )
    write_sync_state(paths.sync_state, last_good)
    synchronizer = ScheduledSynchronizer(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        sync_state_path=paths.sync_state,
        freshness_path=paths.freshness,
        observer=RaisingObserver(requests.ConnectionError("network down")),
        config=SyncConfig(failure_retry_seconds=60),
    )

    result = synchronizer.sync_once(now=LATER)

    assert result.status == "failed"
    assert result.next_sync_at == "2026-08-24T02:16:00Z"
    assert read_sync_state(paths.sync_state, REPO) == last_good
    freshness = read_freshness(paths.freshness, REPO)
    assert freshness.source_status == "stale"
    assert freshness.last_source_sync_at is None
    assert freshness.next_source_sync_at == result.next_sync_at
    assert "ConnectionError" in (freshness.source_error or "")


def test_repository_sync_checkpoints_are_isolated(tmp_path):
    first = repo_paths(tmp_path, "alpha/one")
    second = repo_paths(tmp_path, "beta/two")

    write_sync_state(first.sync_state, RepoSyncState(repo="alpha/one", last_observed_at=NOW))

    assert first.sync_state != second.sync_state
    assert read_sync_state(first.sync_state, "alpha/one").last_observed_at == NOW
    assert read_sync_state(second.sync_state, "beta/two").last_observed_at is None


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None):  # noqa: ANN001
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def raise_for_status(self):  # noqa: ANN201
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):  # noqa: ANN201
        return self.payload


class FakeSession:
    def __init__(self, outcomes):  # noqa: ANN001
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _raw_issue(title: str = "Good first issue") -> dict:
    return {
        "number": 7,
        "title": title,
        "body": "Small deterministic fix.",
        "state": "open",
        "labels": [{"name": "help wanted"}],
        "assignees": [],
        "locked": False,
        "user": {"login": "maintainer"},
        "html_url": f"https://github.com/{REPO}/issues/7",
        "created_at": NOW,
        "updated_at": NOW,
        "closed_at": None,
    }


def _http_config(**updates) -> SyncConfig:  # noqa: ANN003
    values = {
        "item_limit": 1,
        "comment_limit_per_item": 0,
        "dependency_limit_per_issue": 0,
        "http_attempts": 2,
        "http_backoff_seconds": 1,
    }
    values.update(updates)
    return SyncConfig(**values)


def test_conditional_get_reuses_cached_representation_on_304():
    session = FakeSession(
        [
            FakeResponse(200, [_raw_issue()], {"ETag": '"issues-v1"'}),
            FakeResponse(304),
        ]
    )
    client = ConditionalGitHubClient(session=session, sleep=lambda _: None)
    config = _http_config()

    first = client.observe(REPO, RepoSyncState(repo=REPO), config, NOW)
    previous = RepoSyncState(
        repo=REPO,
        request_cache=first.request_cache,
        resources=first.resources,
    )
    second = client.observe(REPO, previous, config, LATER)

    # ``dependency_limit_per_issue=0`` reads no blocked_by page, so this poll
    # made no dependency observation at all.
    assert set(first.resources) == set(second.resources) == {"issue:7"}
    assert second.not_modified_requests == 1
    assert session.calls[1][1]["headers"]["If-None-Match"] == '"issues-v1"'
    assert client.write_request_count == 0


def test_transient_http_failure_retries_serially_with_exponential_backoff():
    session = FakeSession(
        [
            requests.ConnectionError("network down"),
            FakeResponse(200, [_raw_issue()], {"ETag": '"issues-v1"'}),
        ]
    )
    delays: list[float] = []
    client = ConditionalGitHubClient(session=session, sleep=delays.append)

    observed = client.observe(REPO, RepoSyncState(repo=REPO), _http_config(), NOW)

    assert "issue:7" in observed.resources
    assert delays == [1]
    assert len(session.calls) == 2


def test_rate_limit_headers_produce_an_exact_non_blocking_retry_time():
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    reset = int(datetime(2026, 8, 24, 3, 0, tzinfo=timezone.utc).timestamp())
    session = FakeSession(
        [
            FakeResponse(
                429,
                {"message": "rate limited"},
                {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
            )
        ]
    )
    client = ConditionalGitHubClient(
        session=session,
        sleep=lambda _: pytest.fail("rate limits must not block the process"),
        clock=lambda: now,
    )

    with pytest.raises(RateLimitedError) as caught:
        client.observe(REPO, RepoSyncState(repo=REPO), _http_config(), NOW)

    assert caught.value.retry_at == "2026-08-24T03:00:00Z"


def test_x_poll_interval_extends_visible_next_sync_time(tmp_path):
    observer = StaticObserver(_observation([_issue_resource()], poll_interval=1200))
    synchronizer, paths = _synchronizer(
        tmp_path,
        observer,
        config=SyncConfig(interval_seconds=900),
    )

    result = synchronizer.sync_once(now=NOW)

    assert result.next_sync_at == "2026-08-24T02:20:00Z"
    freshness = read_freshness(paths.freshness, REPO)
    assert freshness.sync_interval_seconds == 1200


def _blocked_by_added_webhook(count: int) -> RepoEvent:
    return RepoEvent(
        delivery_id=f"webhook-dependency-{count}",
        event_type="issue_dependencies",
        action="blocked_by_added",
        repo=REPO,
        received_at=NOW,
        payload={
            "action": "blocked_by_added",
            "repository": {"full_name": REPO},
            "blocked_issue": {
                "number": 7,
                "repository_url": f"https://api.github.com/repos/{REPO}",
            },
        },
        attachments={"blocking_dependency_count": count},
    )


def _drain(paths, now: str) -> None:  # noqa: ANN001
    processor = DeliveryProcessor(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        state_path=paths.state,
        event_log=EventLog(paths.event_log),
        extractor=NullExtractor(),
        freshness_path=paths.freshness,
    )
    while True:
        processed = processor.process_one(now=now)
        if processed is None:
            return
        assert processed.status == "succeeded", processed.error


def test_first_observation_of_no_blockers_repairs_a_missed_removal(tmp_path):
    """The safety net exists for exactly this drift, so its first poll must fix it.

    A ``blocked_by_removed`` webhook never arrived, so live state still calls the
    issue blocked. The checkpoint is empty on the first scheduled poll, and it
    describes GitHub, never live state: treating that first fully observed zero
    as a mere baseline would leave the stale block in place for good, because
    every later poll sees an unchanged zero and emits nothing.
    """
    paths = RepoRegistry(tmp_path).register(REPO)
    inbox = DeliveryInbox(paths.inbox)
    inbox.enqueue(_blocked_by_added_webhook(2), now=NOW)
    _drain(paths, NOW)
    assert read_state(paths.state).items[f"{REPO}#issue-7"].blocking_dependency_count == 2

    observer = StaticObserver(
        _observation([_issue_resource(updated_at=LATER), _dependency_resource(0, updated_at=LATER)])
    )
    synchronizer, _ = _synchronizer(tmp_path, observer, inbox=DeliveryInbox(paths.inbox))
    result = synchronizer.sync_once(now=LATER)
    assert result.status == "succeeded"
    _drain(paths, LATER)

    state = read_state(paths.state)
    assert state.items[f"{REPO}#issue-7"].blocking_dependency_count == 0
    assert opportunities(project_graph(state))[0].status == "available"


def test_incomplete_dependency_window_is_not_a_zero_observation():
    """An unread blocked_by page must never publish a blocked issue as available."""
    session = FakeSession(
        [
            FakeResponse(200, [_raw_issue()]),
            FakeResponse(
                200,
                [{"number": 11, "state": "closed"}, {"number": 12, "state": "closed"}],
            ),
        ]
    )
    client = ConditionalGitHubClient(session=session, sleep=lambda _: None)
    config = _http_config(dependency_limit_per_issue=2, dependency_max_pages=1)

    observed = client.observe(REPO, RepoSyncState(repo=REPO), config, NOW)

    assert "dependency:7" not in observed.resources
    previous = RepoSyncState(repo=REPO, resources={"dependency:7": _dependency_resource(3)})
    plan = plan_reconciliation(REPO, previous, observed)
    assert [event.event_type for event in plan.events] == ["issues"]
    assert plan.resources["dependency:7"].attachments["blocking_dependency_count"] == 3


def test_incomplete_file_window_leaves_the_file_set_to_worker_hydration():
    session = FakeSession(
        [
            FakeResponse(200, [{**_raw_issue(), "pull_request": {}}]),
            FakeResponse(200, {"number": 7, "merged": False, "draft": False}),
            FakeResponse(200, [{"filename": "src/a.py"}, {"filename": "src/b.py"}]),
        ]
    )
    client = ConditionalGitHubClient(session=session, sleep=lambda _: None)
    config = _http_config(file_limit_per_pull=2, file_max_pages=1)

    observed = client.observe(REPO, RepoSyncState(repo=REPO), config, NOW)

    assert "files" not in observed.resources["pull_request:7"].attachments


@pytest.mark.parametrize(
    "headers",
    [
        {"Retry-After": "0"},
        {"Retry-After": "Mon, 24 Aug 2026 01:30:00 GMT"},
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(
                int(datetime(2026, 8, 24, 1, 30, tzinfo=timezone.utc).timestamp())
            ),
        },
    ],
)
def test_non_future_rate_limit_hints_use_a_positive_fallback(headers):  # noqa: ANN001
    """A stale or zero hint must not turn a refusal into a hot loop."""
    now = datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)
    session = FakeSession(
        [
            FakeResponse(
                403,
                {"message": "rate limited"},
                headers,
            )
        ]
    )
    client = ConditionalGitHubClient(
        session=session,
        sleep=lambda _: pytest.fail("rate limits must not block the process"),
        clock=lambda: now,
    )

    with pytest.raises(RateLimitedError) as caught:
        client.observe(REPO, RepoSyncState(repo=REPO), _http_config(), NOW)

    assert caught.value.retry_at == "2026-08-24T02:01:00Z"
