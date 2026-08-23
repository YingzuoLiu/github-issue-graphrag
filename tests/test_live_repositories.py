from __future__ import annotations

import pytest

from issue_graphrag.live.events import EventLog, normalize_envelope
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.indexer import NullExtractor
from issue_graphrag.live.processor import DeliveryProcessor
from issue_graphrag.live.repositories import (
    RepoFreshness,
    RepoRegistry,
    canonical_repo,
    read_freshness,
    repo_paths,
    write_freshness,
)
from issue_graphrag.live.store import read_state

NOW = "2026-08-23T02:00:00Z"


def _event(repo: str, delivery_id: str, number: int):
    return normalize_envelope(
        {
            "delivery_id": delivery_id,
            "event_type": "issues",
            "received_at": NOW,
            "payload": {
                "action": "opened",
                "repository": {"full_name": repo},
                "issue": {
                    "number": number,
                    "title": f"Issue {number}",
                    "body": "",
                    "state": "open",
                    "labels": [],
                    "assignees": [],
                    "user": {"login": "author"},
                    "html_url": f"https://github.com/{repo}/issues/{number}",
                    "created_at": NOW,
                    "updated_at": NOW,
                },
            },
        }
    )


class FailingExtractor:
    def extract(self, text_units):  # noqa: ANN001, ANN201
        raise RuntimeError("semantic provider unavailable")


def _processor(paths, extractor):  # noqa: ANN001
    return DeliveryProcessor(
        repo=paths.repo,
        inbox=DeliveryInbox(paths.inbox),
        state_path=paths.state,
        event_log=EventLog(paths.event_log),
        extractor=extractor,
        lease_seconds=30,
        retry_delay_seconds=0,
        max_attempts=1,
        freshness_path=paths.freshness,
    )


def test_repo_paths_are_canonical_and_cannot_escape_the_root(tmp_path):
    paths = repo_paths(tmp_path, "Owner/Repo.Name")

    assert canonical_repo("Owner/Repo.Name") == "owner/repo.name"
    assert paths.root == tmp_path / "owner__repo.name"
    assert paths.state.parent == paths.inbox.parent == paths.extraction_cache.parent
    assert paths.root.is_relative_to(tmp_path)

    for invalid in ("../repo", "owner/..", "owner/repo/name", "owner/repo\\escape"):
        with pytest.raises(ValueError):
            repo_paths(tmp_path, invalid)


def test_registry_and_freshness_keep_two_repositories_independent(tmp_path):
    registry = RepoRegistry(tmp_path, ("Alpha/One",))
    first = registry.register("alpha/one")
    second = registry.register("Beta/Two")

    assert registry.repositories() == ["alpha/one", "beta/two"]
    assert first.root != second.root
    assert first.state != second.state
    assert first.inbox != second.inbox
    assert first.extraction_cache != second.extraction_cache

    write_freshness(
        first.freshness,
        RepoFreshness(repo=first.repo, last_source_sync_at=NOW, semantic_status="pending"),
    )
    assert read_freshness(first.freshness, first.repo).last_source_sync_at == NOW
    assert read_freshness(second.freshness, second.repo).last_source_sync_at is None

    with pytest.raises(ValueError, match="not configured"):
        registry.paths("third/unregistered")


def test_one_repository_failure_does_not_block_another_repository_lane(tmp_path):
    registry = RepoRegistry(tmp_path)
    failed_paths = registry.register("alpha/one")
    healthy_paths = registry.register("beta/two")
    failed_inbox = DeliveryInbox(failed_paths.inbox)
    healthy_inbox = DeliveryInbox(healthy_paths.inbox)
    failed_inbox.enqueue(_event(failed_paths.repo, "failed-1", 1), now=NOW)
    healthy_inbox.enqueue(_event(healthy_paths.repo, "healthy-1", 2), now=NOW)

    failed = _processor(failed_paths, FailingExtractor()).process_one(now=NOW)
    healthy = _processor(healthy_paths, NullExtractor()).process_one(now=NOW)

    assert failed is not None and failed.status == "failed"
    assert healthy is not None and healthy.status == "succeeded"
    assert not failed_paths.state.exists()
    assert read_state(healthy_paths.state).repo == "beta/two"
    assert read_freshness(failed_paths.freshness, failed_paths.repo).semantic_status == "degraded"
    assert read_freshness(healthy_paths.freshness, healthy_paths.repo).semantic_status == "current"
    assert failed_inbox.count("failed") == 1
    assert healthy_inbox.count("succeeded") == 1
