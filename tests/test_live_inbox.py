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


def test_source_deliveries_and_semantic_jobs_share_one_writer_lane(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    inbox.upsert_semantic_job(
        document_id="owner/repo#issue-1",
        content_signature="signature-1",
        trigger_delivery_id="seed",
        total_units=3,
        now=NOW,
    )
    inbox.enqueue(_event("delivery-1"), now=NOW)

    # A ready source observation always wins over enrichment.
    assert inbox.claim_semantic_job(now=NOW, lease_seconds=30) is None
    delivery = inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3)
    assert delivery is not None and delivery.lease_id is not None
    inbox.mark_succeeded("delivery-1", delivery.lease_id, now=NOW)

    semantic = inbox.claim_semantic_job(now=NOW, lease_seconds=30)
    assert semantic is not None and semantic.lease_id is not None
    inbox.enqueue(_event("delivery-2", number=2), now=NOW)
    assert inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3) is None

    inbox.defer_semantic_job(
        semantic.document_id,
        semantic.lease_id,
        "provider unavailable",
        now=NOW,
        retry_delay_seconds=0,
    )
    next_delivery = inbox.claim_next(now=NOW, lease_seconds=30, max_attempts=3)
    assert next_delivery is not None
    assert next_delivery.event.delivery_id == "delivery-2"


def test_new_content_signature_replaces_deferred_cursor_durably(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    document_id = "owner/repo#issue-1"
    assert (
        inbox.upsert_semantic_job(
            document_id=document_id,
            content_signature="signature-1",
            trigger_delivery_id="delivery-1",
            total_units=8,
            now=NOW,
        )
        == "enqueued"
    )
    job = inbox.claim_semantic_job(now=NOW, lease_seconds=30)
    assert job is not None and job.lease_id is not None
    inbox.advance_semantic_job(document_id, job.lease_id, 4, now=NOW)
    inbox.defer_semantic_job(
        document_id,
        job.lease_id,
        "quota",
        now=NOW,
        retry_delay_seconds=0,
    )

    assert (
        inbox.upsert_semantic_job(
            document_id=document_id,
            content_signature="signature-2",
            trigger_delivery_id="delivery-2",
            total_units=2,
            now="2024-06-01T10:00:01Z",
        )
        == "replaced"
    )
    restored = DeliveryInbox(tmp_path / "inbox.sqlite").get_semantic_job(document_id)
    assert restored is not None
    assert restored.content_signature == "signature-2"
    assert restored.next_unit_index == 0
    assert restored.total_units == 2
    assert restored.attempts == 0
    assert restored.status == "pending"


def test_deferred_long_document_rotates_behind_less_attempted_documents(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    for document_id in ("owner/repo#issue-1", "owner/repo#issue-2"):
        inbox.upsert_semantic_job(
            document_id=document_id,
            content_signature=f"signature-{document_id[-1]}",
            trigger_delivery_id="seed",
            total_units=20,
            now=NOW,
        )

    first = inbox.claim_semantic_job(now=NOW, lease_seconds=30)
    assert first is not None and first.lease_id is not None
    inbox.defer_semantic_job(
        first.document_id,
        first.lease_id,
        "batch limit",
        now=NOW,
        retry_delay_seconds=0,
    )

    second = inbox.claim_semantic_job(now=NOW, lease_seconds=30)
    assert second is not None
    assert second.document_id != first.document_id
