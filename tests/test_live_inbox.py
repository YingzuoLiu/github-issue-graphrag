from __future__ import annotations

import pytest
from conftest import issue_payload, make_event

from issue_graphrag.live.inbox import DeliveryConflict, DeliveryInbox, LeaseLostError

NOW = "2024-06-01T10:00:00Z"


def _event(delivery_id: str, number: int = 1):
    return make_event(
        delivery_id,
        "issues",
        {"action": "opened", "issue": issue_payload(number)},
        NOW,
    )


def test_inbox_deduplicates_a_delivery_and_rejects_id_reuse(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    original = _event("delivery-1")

    assert inbox.enqueue(original, now=NOW).outcome == "enqueued"
    assert inbox.enqueue(original, now=NOW).outcome == "duplicate"

    with pytest.raises(DeliveryConflict, match="different payload"):
        inbox.enqueue(_event("delivery-1", number=2), now=NOW)

    stored = inbox.get("delivery-1")
    assert stored is not None
    assert stored.event.payload["issue"]["number"] == 1
    assert inbox.count() == 1


def test_only_one_delivery_can_be_processing_and_an_expired_lease_is_reclaimed(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    inbox.enqueue(_event("delivery-1"), now=NOW)
    inbox.enqueue(_event("delivery-2"), now=NOW)

    first = inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3)
    assert first is not None
    assert first.event.delivery_id == "delivery-1"
    assert first.attempts == 1
    assert inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3) is None

    reclaimed = inbox.claim_next(
        now="2024-06-01T10:00:31Z",
        lease_seconds=30,
        max_attempts=3,
    )
    assert reclaimed is not None
    assert reclaimed.event.delivery_id == "delivery-1"
    assert reclaimed.attempts == 2


def test_heartbeat_extends_the_lease_and_fences_the_previous_worker(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    inbox.enqueue(_event("delivery-1"), now=NOW)
    first = inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3)
    assert first is not None and first.lease_id is not None

    inbox.renew_lease(
        "delivery-1",
        first.lease_id,
        now="2024-06-01T10:00:20Z",
    )
    assert (
        inbox.claim_next(
            now="2024-06-01T10:00:31Z",
            lease_seconds=30,
            max_attempts=3,
        )
        is None
    )

    second = inbox.claim_next(
        now="2024-06-01T10:00:51Z",
        lease_seconds=30,
        max_attempts=3,
    )
    assert second is not None and second.lease_id != first.lease_id
    with pytest.raises(LeaseLostError):
        inbox.mark_succeeded("delivery-1", first.lease_id, now="2024-06-01T10:00:52Z")


def test_failed_delivery_retries_then_redelivery_reopens_the_dead_letter(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = _event("delivery-1")
    inbox.enqueue(event, now=NOW)

    claimed = inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=1)
    assert claimed is not None
    outcome = inbox.mark_failed(
        "delivery-1",
        claimed.lease_id,
        "boom",
        now=NOW,
        retry_delay_seconds=0,
        max_attempts=1,
    )
    assert outcome == "failed"
    assert inbox.get("delivery-1").status == "failed"  # type: ignore[union-attr]

    assert inbox.enqueue(event, now="2024-06-01T10:01:00Z").outcome == "requeued"
    retried = inbox.claim_next(
        now="2024-06-01T10:01:00Z",
        lease_seconds=30,
        max_attempts=1,
    )
    assert retried is not None
    assert retried.attempts == 1


def test_enriched_event_is_durable_before_processing(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    event = _event("delivery-1")
    inbox.enqueue(event, now=NOW)
    claimed = inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3)
    assert claimed is not None

    claimed.event.attachments["files"] = ["src/a.py"]
    claimed.event.indexed_at = "2024-06-01T10:00:01Z"
    inbox.update_event(
        claimed.event,
        lease_id=claimed.lease_id,
        now="2024-06-01T10:00:01Z",
    )

    reopened = DeliveryInbox(tmp_path / "inbox.sqlite").get("delivery-1")
    assert reopened is not None
    assert reopened.event.attachments == {"files": ["src/a.py"]}
    assert reopened.event.indexed_at == "2024-06-01T10:00:01Z"
