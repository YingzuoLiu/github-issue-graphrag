"""Delivery handling: signatures, envelopes, the event log and record updates."""

from __future__ import annotations

import json

import pytest
from conftest import REPO, issue_payload, make_event, pull_payload

from issue_graphrag.live.events import EventLog, normalize_envelope
from issue_graphrag.live.models import LiveState
from issue_graphrag.live.records import UnsupportedEvent, apply_event_to_records
from issue_graphrag.live.webhook import (
    WebhookError,
    compute_signature,
    parse_webhook,
    verify_signature,
)

SECRET = "s3cret"


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_verify_signature_accepts_only_the_matching_digest():
    body = _body({"action": "opened"})

    assert verify_signature(SECRET, body, compute_signature(SECRET, body))
    assert not verify_signature(SECRET, body, compute_signature("wrong", body))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha256=deadbeef")


def test_parse_webhook_rejects_a_tampered_body():
    original = _body({"action": "opened", "repository": {"full_name": REPO}})
    signature = compute_signature(SECRET, original)
    tampered = _body({"action": "closed", "repository": {"full_name": REPO}})

    headers = {
        "X-GitHub-Delivery": "d-1",
        "X-GitHub-Event": "issues",
        "X-Hub-Signature-256": signature,
    }

    with pytest.raises(WebhookError, match="signature"):
        parse_webhook(headers, tampered, secret=SECRET)


def test_parse_webhook_requires_delivery_and_event_headers():
    body = _body({"repository": {"full_name": REPO}})

    with pytest.raises(WebhookError, match="X-GitHub-Delivery"):
        parse_webhook({"X-GitHub-Event": "issues"}, body)

    with pytest.raises(WebhookError, match="X-GitHub-Event"):
        parse_webhook({"X-GitHub-Delivery": "d-1"}, body)


def test_parse_webhook_is_case_insensitive_about_headers():
    payload = {"action": "opened", "repository": {"full_name": REPO}}
    body = _body(payload)
    headers = {
        "x-github-delivery": "d-9",
        "x-github-event": "issues",
        "x-hub-signature-256": compute_signature(SECRET, body),
    }

    event = parse_webhook(headers, body, secret=SECRET)

    assert (event.delivery_id, event.event_type, event.action) == ("d-9", "issues", "opened")
    assert event.repo == REPO


def test_parse_webhook_turns_non_object_or_incomplete_json_into_webhook_errors():
    for payload in ([], {"action": "opened"}):
        body = _body(payload)
        headers = {
            "X-GitHub-Delivery": "d-bad",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": compute_signature(SECRET, body),
        }

        with pytest.raises(WebhookError):
            parse_webhook(headers, body, secret=SECRET)


def test_normalize_envelope_derives_the_timestamp_from_the_payload():
    event = normalize_envelope(
        {
            "headers": {"X-GitHub-Delivery": "d-2", "X-GitHub-Event": "issue_comment"},
            "payload": {
                "action": "created",
                "repository": {"full_name": REPO},
                "issue": {"number": 1, "updated_at": "2024-05-01T00:00:00Z"},
                "comment": {"id": 5, "created_at": "2024-05-02T10:30:00Z"},
            },
        }
    )

    assert event.received_at == "2024-05-02T10:30:00Z"
    assert event.source == "webhook"
    assert event.observation_label() == "Received via GitHub Webhook"


def test_event_log_round_trips_and_reports_delivery_ids(tmp_path):
    log = EventLog(tmp_path / "event_log.jsonl")
    first = make_event("d-1", "issues", {"action": "opened", "issue": issue_payload(1)}, "2024-05-01T00:00:00Z")
    second = make_event("d-2", "issues", {"action": "closed", "issue": issue_payload(1)}, "2024-05-02T00:00:00Z")

    log.extend([first, second])

    assert [event.delivery_id for event in log.read_all()] == ["d-1", "d-2"]
    assert log.delivery_ids() == {"d-1", "d-2"}


def test_event_log_append_once_does_not_duplicate_a_delivery(tmp_path):
    log = EventLog(tmp_path / "event_log.jsonl")
    event = make_event(
        "d-1",
        "issues",
        {"action": "opened", "issue": issue_payload(1)},
        "2024-05-01T00:00:00Z",
    )

    assert log.append_once(event)
    assert not log.append_once(event)
    assert [stored.delivery_id for stored in log.read_all()] == ["d-1"]


def test_event_log_append_once_uses_a_warm_delivery_index(tmp_path, monkeypatch):
    path = tmp_path / "event_log.jsonl"
    log = EventLog(path)
    first = make_event(
        "d-1",
        "issues",
        {"action": "opened", "issue": issue_payload(1)},
        "2024-05-01T00:00:00Z",
    )
    second = make_event(
        "d-2",
        "issues",
        {"action": "opened", "issue": issue_payload(2)},
        "2024-05-01T00:00:01Z",
    )

    assert log.append_once(first)

    def unexpected_rescan():
        raise AssertionError("append_once rescanned the complete event log")

    monkeypatch.setattr(log, "read_all", unexpected_rescan)
    assert log.append_once(second)
    assert not log.append_once(first)
    assert EventLog(path).delivery_ids() == {"d-1", "d-2"}


def test_event_log_repairs_a_truncated_final_write_before_retry(tmp_path):
    path = tmp_path / "event_log.jsonl"
    path.write_text('{"delivery_id":"cut off', encoding="utf-8")
    log = EventLog(path)
    event = make_event(
        "d-1",
        "issues",
        {"action": "opened", "issue": issue_payload(1)},
        "2024-05-01T00:00:00Z",
    )

    assert log.append_once(event)
    assert [stored.delivery_id for stored in log.read_all()] == ["d-1"]


def test_deleting_a_comment_removes_it_from_the_record():
    state = LiveState(repo=REPO)
    comment = {"id": 42, "body": "keep me", "user": {"login": "a"}, "created_at": "2024-05-01T00:00:00Z"}

    created = make_event(
        "d-1", "issue_comment",
        {"action": "created", "issue": issue_payload(7), "comment": comment},
        "2024-05-01T00:00:00Z",
    )
    apply_event_to_records(state, created)
    document_id = f"{REPO}#issue-7"
    assert list(state.items[document_id].comments) == ["42"]

    deleted = make_event(
        "d-2", "issue_comment",
        {"action": "deleted", "issue": issue_payload(7), "comment": comment},
        "2024-05-02T00:00:00Z",
    )
    apply_event_to_records(state, deleted)

    assert state.items[document_id].comments == {}


def test_a_comment_on_a_pull_request_lands_on_the_pull_document():
    """GitHub delivers pull request comments under the ``issue`` key."""
    state = LiveState(repo=REPO)
    parent = {**issue_payload(950), "pull_request": {"url": "https://api.github.com/pulls/950"}}

    event = make_event(
        "d-1", "issue_comment",
        {"action": "created", "issue": parent, "comment": {"id": 1, "body": "hi", "user": {"login": "a"}}},
        "2024-05-01T00:00:00Z",
    )
    affected = apply_event_to_records(state, event)

    assert affected == [f"{REPO}#pull-950"]
    assert state.items[affected[0]].kind == "pull_request"
    assert state.items[affected[0]].node_name == "PR #950"


def test_a_comment_on_a_pull_request_can_carry_hydrated_files():
    state = LiveState(repo=REPO)
    parent = {**issue_payload(950), "pull_request": {"url": "https://api.github.com/pulls/950"}}
    event = make_event(
        "d-1",
        "issue_comment",
        {"action": "created", "issue": parent, "comment": {"id": 1, "body": "hi"}},
        "2024-05-01T00:00:00Z",
        attachments={"files": ["src/live.py"]},
    )

    apply_event_to_records(state, event)

    assert state.items[f"{REPO}#pull-950"].files == ["src/live.py"]


def test_merge_state_is_taken_from_the_payload():
    state = LiveState(repo=REPO)
    event = make_event(
        "d-1", "pull_request",
        {
            "action": "closed",
            "pull_request": pull_payload(950, state="closed", merged=True, merged_at="2024-05-06T15:00:00Z"),
        },
        "2024-05-06T15:00:00Z",
        attachments={"files": ["a/b.py"]},
    )
    apply_event_to_records(state, event)

    item = state.items[f"{REPO}#pull-950"]
    assert item.lifecycle_state() == "merged"
    assert item.files == ["a/b.py"]


def test_assignees_are_presence_merged_and_a_stale_payload_cannot_restore_them():
    state = LiveState(repo=REPO)
    assigned = make_event(
        "d-1",
        "issues",
        {
            "action": "assigned",
            "issue": issue_payload(
                7,
                assignees=[{"login": "octocat"}, {"login": "hubot"}],
                updated_at="2024-05-01T00:00:00Z",
            ),
        },
        "2024-05-01T00:00:00Z",
    )
    apply_event_to_records(state, assigned)
    document_id = f"{REPO}#issue-7"
    assert state.items[document_id].assignees == ["hubot", "octocat"]

    # A partial payload that omits assignees must not erase the known set.
    partial = issue_payload(7, updated_at="2024-05-02T00:00:00Z")
    partial.pop("labels")
    partial.pop("user")
    apply_event_to_records(
        state,
        make_event(
            "d-2",
            "issues",
            {"action": "edited", "issue": partial},
            "2024-05-02T00:00:00Z",
        ),
    )
    assert state.items[document_id].assignees == ["hubot", "octocat"]

    apply_event_to_records(
        state,
        make_event(
            "d-3",
            "issues",
            {
                "action": "unassigned",
                "issue": issue_payload(
                    7,
                    assignees=[],
                    updated_at="2024-05-03T00:00:00Z",
                ),
            },
            "2024-05-03T00:00:00Z",
        ),
    )
    apply_event_to_records(state, assigned.model_copy(update={"delivery_id": "d-late"}))

    assert state.items[document_id].assignees == []


def test_unknown_event_types_are_refused():
    state = LiveState(repo=REPO)
    event = make_event("d-1", "release", {"action": "published"}, "2024-05-01T00:00:00Z")

    with pytest.raises(UnsupportedEvent):
        apply_event_to_records(state, event)
