"""A dependency-free HTTP boundary for GitHub webhook deliveries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from issue_graphrag.live.inbox import DeliveryConflict, DeliveryInbox
from issue_graphrag.live.records import supports_event
from issue_graphrag.live.timeutil import now_utc, to_iso
from issue_graphrag.live.webhook import (
    WebhookAuthenticationError,
    WebhookError,
    parse_webhook,
)

DEFAULT_MAX_BODY_BYTES = 25 * 1024 * 1024
DEFAULT_READ_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    body: dict[str, Any]


class WebhookReceiver:
    """Verify, normalize and durably enqueue; never run extraction in HTTP."""

    def __init__(
        self,
        secret: str,
        repo: str,
        inbox: DeliveryInbox,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ):
        if not secret:
            raise ValueError("a non-empty GitHub webhook secret is required")
        parts = repo.strip().split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError("repo must be in 'owner/name' format")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        self.secret = secret
        self.repo = "/".join(parts)
        self.inbox = inbox
        self.max_body_bytes = max_body_bytes

    def receive(
        self,
        headers: Mapping[str, str],
        body: bytes,
        received_at: str | None = None,
    ) -> WebhookResponse:
        if len(body) > self.max_body_bytes:
            return WebhookResponse(413, {"status": "rejected", "error": "payload too large"})

        moment = to_iso(received_at) if received_at else to_iso(now_utc())
        try:
            event = parse_webhook(
                headers,
                body,
                secret=self.secret,
                received_at=moment,
            )
        except WebhookAuthenticationError as exc:
            return WebhookResponse(401, {"status": "rejected", "error": str(exc)})
        except WebhookError as exc:
            return WebhookResponse(400, {"status": "rejected", "error": str(exc)})

        if event.repo.casefold() != self.repo.casefold():
            return WebhookResponse(403, {"status": "rejected", "error": "repository not allowed"})
        event.repo = self.repo
        if event.event_type == "ping":
            return WebhookResponse(200, {"status": "ok", "delivery_id": event.delivery_id})
        if not supports_event(event.event_type, event.action):
            return WebhookResponse(
                202,
                {
                    "status": "ignored",
                    "delivery_id": event.delivery_id,
                    "event": event.summary(),
                },
            )

        try:
            result = self.inbox.enqueue(event, now=moment)
        except DeliveryConflict as exc:
            return WebhookResponse(409, {"status": "rejected", "error": str(exc)})
        except (OSError, sqlite3.Error):
            return WebhookResponse(
                503,
                {"status": "unavailable", "error": "durable inbox unavailable"},
            )
        return WebhookResponse(
            202,
            {"delivery_id": event.delivery_id, "status": result.outcome},
        )


class _WebhookHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def create_http_server(
    receiver: WebhookReceiver,
    host: str = "127.0.0.1",
    port: int = 8000,
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
) -> ThreadingHTTPServer:
    """Adapt ``WebhookReceiver`` to the Python standard-library HTTP server."""
    if read_timeout_seconds <= 0:
        raise ValueError("read_timeout_seconds must be positive")

    class Handler(BaseHTTPRequestHandler):
        # StreamRequestHandler.setup() applies this to the accepted socket before
        # request headers or the signed body are read. An unauthenticated peer can
        # therefore hold a handler thread only for this bounded interval.
        timeout = read_timeout_seconds

        def _send(self, response: WebhookResponse) -> None:
            raw = json.dumps(response.body, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/healthz":
                self._send(WebhookResponse(404, {"status": "not found"}))
                return
            try:
                receiver.inbox.count()
            except Exception:
                self._send(WebhookResponse(503, {"status": "unavailable"}))
                return
            self._send(WebhookResponse(200, {"status": "ok"}))

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/webhooks/github":
                self._send(WebhookResponse(404, {"status": "not found"}))
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send(WebhookResponse(400, {"status": "rejected", "error": "bad length"}))
                return
            if length < 0:
                self._send(WebhookResponse(400, {"status": "rejected", "error": "bad length"}))
                return
            if length > receiver.max_body_bytes:
                self._send(
                    WebhookResponse(413, {"status": "rejected", "error": "payload too large"})
                )
                return
            body = self.rfile.read(length)
            headers = {key: value for key, value in self.headers.items()}
            self._send(receiver.receive(headers, body))

        def log_message(self, format: str, *args: Any) -> None:
            return

    return _WebhookHTTPServer((host, port), Handler)
