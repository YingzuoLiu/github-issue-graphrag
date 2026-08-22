from __future__ import annotations

from typing import Any

from issue_graphrag.live.models import Comment, LiveState, RepoEvent, RepoItem
from issue_graphrag.live.timeutil import to_iso

#: Event types the incremental indexer knows how to apply.
SUPPORTED_EVENTS = ("issues", "issue_comment", "pull_request")


class UnsupportedEvent(ValueError):
    """Raised for a delivery the incremental indexer intentionally ignores."""


def _labels(payload: dict[str, Any]) -> list[str]:
    labels = payload.get("labels") or []
    names = [str(label.get("name", "")).strip() for label in labels if isinstance(label, dict)]
    return sorted({name for name in names if name})


def _login(payload: dict[str, Any], key: str = "user") -> str:
    user = payload.get(key) or {}
    return str(user.get("login", "")) if isinstance(user, dict) else ""


def _timestamp(value: Any) -> str | None:
    return to_iso(str(value)) if value else None


def _kind_of(payload: dict[str, Any], default: str) -> str:
    """A comment on a pull request arrives under the ``issue`` key."""
    return "pull_request" if payload.get("pull_request") is not None else default


def _document_id(repo: str, kind: str, number: int) -> str:
    slug = "issue" if kind == "issue" else "pull"
    return f"{repo}#{slug}-{number}"


def upsert_item(
    state: LiveState,
    repo: str,
    payload: dict[str, Any],
    kind: str,
    files: list[str] | None = None,
) -> str:
    """Apply the authoritative issue/PR payload onto the stored record."""
    number = int(payload["number"])
    document_id = _document_id(repo, kind, number)
    existing = state.items.get(document_id)

    merged_at = _timestamp(payload.get("merged_at"))
    merged = bool(payload.get("merged")) or merged_at is not None

    item = RepoItem(
        kind="issue" if kind == "issue" else "pull_request",
        repo=repo,
        number=number,
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        state=str(payload.get("state") or "open"),
        merged=merged,
        draft=bool(payload.get("draft")),
        labels=_labels(payload),
        author=_login(payload),
        url=payload.get("html_url") or (existing.url if existing else None),
        created_at=_timestamp(payload.get("created_at")),
        updated_at=_timestamp(payload.get("updated_at")),
        closed_at=_timestamp(payload.get("closed_at")),
        merged_at=merged_at,
        comments=dict(existing.comments) if existing else {},
        files=sorted(files) if files is not None else (list(existing.files) if existing else []),
    )

    state.items[document_id] = item
    return document_id


def upsert_comment(state: LiveState, document_id: str, payload: dict[str, Any]) -> None:
    item = state.items[document_id]
    comment_id = str(payload.get("id"))
    item.comments[comment_id] = Comment(
        id=comment_id,
        author=_login(payload),
        body=str(payload.get("body") or ""),
        url=payload.get("html_url"),
        created_at=_timestamp(payload.get("created_at")),
        updated_at=_timestamp(payload.get("updated_at")),
    )


def remove_comment(state: LiveState, document_id: str, payload: dict[str, Any]) -> None:
    item = state.items[document_id]
    item.comments.pop(str(payload.get("id")), None)


def apply_event_to_records(state: LiveState, event: RepoEvent) -> list[str]:
    """Update stored records from one delivery and return the documents touched.

    Only GitHub's own payload is trusted here. Nothing in this function calls an
    LLM: issue state, pull request state, labels and comment bodies are facts
    GitHub already states.
    """
    payload = event.payload
    repo = event.repo

    if event.event_type == "issues":
        issue = payload.get("issue") or {}
        if not issue:
            raise UnsupportedEvent("issues event without an issue payload")
        return [upsert_item(state, repo, issue, "issue")]

    if event.event_type == "pull_request":
        pull = payload.get("pull_request") or {}
        if not pull:
            raise UnsupportedEvent("pull_request event without a pull_request payload")
        files = event.attachments.get("files")
        files = [str(path) for path in files] if isinstance(files, list) else None
        return [upsert_item(state, repo, pull, "pull_request", files=files)]

    if event.event_type == "issue_comment":
        parent = payload.get("issue") or payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        if not parent or not comment:
            raise UnsupportedEvent("issue_comment event without an issue or comment payload")

        kind = _kind_of(parent, "issue")
        document_id = upsert_item(state, repo, parent, kind)

        if event.action == "deleted":
            remove_comment(state, document_id, comment)
        else:
            upsert_comment(state, document_id, comment)
        return [document_id]

    raise UnsupportedEvent(f"unsupported event type: {event.event_type}")


def seed_items(repo: str, records: list[dict[str, Any]]) -> dict[str, RepoItem]:
    """Build the starting record set from a repository snapshot."""
    items: dict[str, RepoItem] = {}
    for record in records:
        item = RepoItem.model_validate({**record, "repo": record.get("repo", repo)})
        items[item.document_id] = item
    return items
