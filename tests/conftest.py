from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from issue_graphrag.live.events import load_events, normalize_envelope
from issue_graphrag.live.extraction import FixtureExtractor
from issue_graphrag.live.indexer import bootstrap
from issue_graphrag.live.records import seed_items

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "live_demo"
REPO = "trustgraph-ai/trustgraph"


@pytest.fixture
def extractor():
    """A fresh extractor per test so call counts stay meaningful."""
    return FixtureExtractor.from_path(FIXTURES / "extraction_rules.json")


@pytest.fixture
def snapshot():
    with (FIXTURES / "seed.json").open("r", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture
def seeded_state(snapshot, extractor):
    return bootstrap(snapshot["repo"], seed_items(snapshot["repo"], snapshot["items"]), extractor)


@pytest.fixture
def demo_events():
    return load_events(FIXTURES / "events")


@pytest.fixture
def new_extractor():
    def build():
        return FixtureExtractor.from_path(FIXTURES / "extraction_rules.json")

    return build


def make_event(
    delivery_id: str,
    event_type: str,
    payload: dict[str, Any],
    received_at: str,
    attachments: dict[str, Any] | None = None,
):
    """Build a delivery envelope the way a webhook handler would."""
    return normalize_envelope(
        {
            "headers": {"X-GitHub-Delivery": delivery_id, "X-GitHub-Event": event_type},
            "received_at": received_at,
            "payload": {**payload, "repository": {"full_name": REPO}},
            "attachments": attachments or {},
        }
    )


def issue_payload(number: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "number": number,
        "title": f"Issue {number}",
        "body": "",
        "state": "open",
        "labels": [],
        "user": {"login": "someone"},
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "created_at": "2024-04-01T00:00:00Z",
        "updated_at": "2024-04-01T00:00:00Z",
    }
    return {**base, **overrides}


def pull_payload(number: int, **overrides: Any) -> dict[str, Any]:
    base = {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "state": "open",
        "draft": False,
        "merged": False,
        "labels": [],
        "user": {"login": "someone"},
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "created_at": "2024-04-01T00:00:00Z",
        "updated_at": "2024-04-01T00:00:00Z",
    }
    return {**base, **overrides}
