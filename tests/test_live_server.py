from __future__ import annotations

import http.client
import json
import socket
import threading

from conftest import REPO, issue_payload

from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.repositories import RepoRegistry, repo_directory_name
from issue_graphrag.live.server import WebhookReceiver, create_http_server
from issue_graphrag.live.webhook import compute_signature

SECRET = "correct horse battery staple"
NOW = "2024-06-01T10:00:00Z"


def _request(delivery_id: str = "delivery-1", event_type: str = "issues"):
    payload = {
        "action": "opened",
        "repository": {"full_name": REPO},
        "issue": issue_payload(7),
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Delivery": delivery_id,
        "X-GitHub-Event": event_type,
        "X-Hub-Signature-256": compute_signature(SECRET, body),
    }
    return headers, body


def test_receiver_verifies_and_enqueues_without_processing(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    headers, body = _request()

    response = receiver.receive(headers, body, received_at=NOW)

    assert response.status_code == 202
    assert response.body == {"delivery_id": "delivery-1", "status": "enqueued"}
    stored = inbox.get("delivery-1")
    assert stored is not None
    assert stored.status == "pending"
    assert stored.event.received_at == NOW


def test_receiver_accepts_native_issue_dependency_events(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    payload = {
        "action": "blocked_by_added",
        "repository": {"full_name": REPO},
        "blocked_issue": {
            **issue_payload(7),
            "repository_url": f"https://api.github.com/repos/{REPO}",
        },
        "blocking_issue": issue_payload(8),
    }
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Delivery": "dependency-1",
        "X-GitHub-Event": "issue_dependencies",
        "X-Hub-Signature-256": compute_signature(SECRET, body),
    }

    response = receiver.receive(headers, body, received_at=NOW)

    assert response.status_code == 202
    assert response.body["status"] == "enqueued"
    assert inbox.get("dependency-1") is not None


def test_receiver_refuses_untrusted_wrong_repo_and_conflicting_deliveries(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    headers, body = _request()

    bad_headers = {**headers, "X-Hub-Signature-256": "sha256=bad"}
    assert receiver.receive(bad_headers, body, received_at=NOW).status_code == 401

    wrong = json.loads(body)
    wrong["repository"]["full_name"] = "other/repo"
    wrong_body = json.dumps(wrong).encode()
    wrong_headers = {
        **headers,
        "X-Hub-Signature-256": compute_signature(SECRET, wrong_body),
    }
    assert receiver.receive(wrong_headers, wrong_body, received_at=NOW).status_code == 403

    assert receiver.receive(headers, body, received_at=NOW).status_code == 202
    changed = json.loads(body)
    changed["issue"]["number"] = 8
    changed_body = json.dumps(changed).encode()
    changed_headers = {
        **headers,
        "X-Hub-Signature-256": compute_signature(SECRET, changed_body),
    }
    assert receiver.receive(changed_headers, changed_body, received_at=NOW).status_code == 409


def test_ping_and_unhandled_events_are_acknowledged_without_entering_the_inbox(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)

    ping_headers, ping_body = _request(event_type="ping")
    ping = receiver.receive(ping_headers, ping_body, received_at=NOW)
    assert (ping.status_code, ping.body["status"]) == (200, "ok")

    push_headers, push_body = _request(delivery_id="delivery-2", event_type="push")
    push = receiver.receive(push_headers, push_body, received_at=NOW)
    assert (push.status_code, push.body["status"]) == (202, "ignored")
    assert inbox.count() == 0


def test_receiver_rejects_oversized_payloads_before_parsing(tmp_path):
    receiver = WebhookReceiver(
        secret=SECRET,
        repo=REPO,
        inbox=DeliveryInbox(tmp_path / "inbox.sqlite"),
        max_body_bytes=4,
    )
    headers, body = _request()

    assert receiver.receive(headers, body, received_at=NOW).status_code == 413


def test_receiver_returns_unavailable_when_durable_enqueue_fails(tmp_path, monkeypatch):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    headers, body = _request()

    def fail(*args, **kwargs):
        raise OSError("disk unavailable")

    monkeypatch.setattr(inbox, "enqueue", fail)
    response = receiver.receive(headers, body, received_at=NOW)

    assert response.status_code == 503
    assert inbox.count() == 0


def test_stdlib_http_adapter_exposes_health_and_webhook_routes(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    server = create_http_server(receiver, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/healthz")
        health = connection.getresponse()
        assert health.status == 200
        assert json.loads(health.read()) == {"status": "ok"}

        connection.request("GET", "/readyz")
        readiness = connection.getresponse()
        assert readiness.status == 200
        assert json.loads(readiness.read()) == {"status": "ready"}

        headers, body = _request()
        connection.request("POST", "/webhooks/github", body=body, headers=headers)
        accepted = connection.getresponse()
        assert accepted.status == 202
        assert json.loads(accepted.read())["status"] == "enqueued"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_liveness_stays_up_when_durable_readiness_fails(tmp_path, monkeypatch):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    server = create_http_server(receiver, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setattr(inbox, "count", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        connection.request("GET", "/livez")
        live = connection.getresponse()
        assert live.status == 200
        live.read()
        connection.request("GET", "/readyz")
        ready = connection.getresponse()
        assert ready.status == 503
        assert json.loads(ready.read()) == {"status": "unavailable"}
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_receiver_is_not_ready_during_an_incomplete_restore(tmp_path):
    paths = RepoRegistry(tmp_path).register(REPO)
    inbox = DeliveryInbox(paths.inbox)
    audit = (
        tmp_path
        / ".operations"
        / "restores"
        / repo_directory_name(REPO)
        / "pending.json"
    )
    audit.parent.mkdir(parents=True)
    audit.write_text('{"status":"planned"}\n', encoding="utf-8")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)

    assert receiver.readiness().status_code == 503


def test_http_adapter_times_out_an_incomplete_request_body(tmp_path):
    inbox = DeliveryInbox(tmp_path / "inbox.sqlite")
    receiver = WebhookReceiver(secret=SECRET, repo=REPO, inbox=inbox)
    server = create_http_server(
        receiver,
        host="127.0.0.1",
        port=0,
        read_timeout_seconds=0.1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    try:
        client.sendall(
            b"POST /webhooks/github HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: 10\r\n"
            b"\r\n"
            b"x"
        )
        client.settimeout(2)

        assert client.recv(1) == b""
        assert inbox.count() == 0
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
