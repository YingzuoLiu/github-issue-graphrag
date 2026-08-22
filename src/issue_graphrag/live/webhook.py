from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

from issue_graphrag.live.events import normalize_envelope
from issue_graphrag.live.models import RepoEvent

SIGNATURE_HEADER = "X-Hub-Signature-256"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"

SUPPORTED_EVENTS = ("issues", "issue_comment", "pull_request")


class WebhookError(ValueError):
    """Raised when a delivery cannot be trusted or understood."""


def _lookup(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time check of GitHub's ``X-Hub-Signature-256`` header.

    Signature verification is deliberately deterministic and never delegated to
    the LLM: an unverified delivery must not be able to write facts.
    """
    if not signature:
        return False
    return hmac.compare_digest(compute_signature(secret, body), signature)


def parse_webhook(
    headers: Mapping[str, str],
    body: bytes,
    secret: str | None = None,
    attachments: dict[str, Any] | None = None,
    received_at: str | None = None,
) -> RepoEvent:
    """Validate and normalize a raw GitHub delivery.

    This is framework-agnostic on purpose: an HTTP handler only has to pass the
    request headers and the exact raw body so the signature stays verifiable.
    """
    if secret and not verify_signature(secret, body, _lookup(headers, SIGNATURE_HEADER)):
        raise WebhookError("invalid webhook signature")

    delivery_id = _lookup(headers, DELIVERY_HEADER)
    event_type = _lookup(headers, EVENT_HEADER)
    if not delivery_id:
        raise WebhookError(f"missing {DELIVERY_HEADER} header")
    if not event_type:
        raise WebhookError(f"missing {EVENT_HEADER} header")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookError("webhook body is not valid JSON") from exc

    envelope = {
        "headers": {DELIVERY_HEADER: delivery_id, EVENT_HEADER: event_type},
        "payload": payload,
        "attachments": attachments or {},
    }
    if received_at:
        envelope["received_at"] = received_at

    return normalize_envelope(envelope)
