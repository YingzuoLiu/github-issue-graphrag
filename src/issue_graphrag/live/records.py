"""Source records: the repository as GitHub currently describes it.

Two clocks matter here and they are deliberately separate:

- ``effective_at`` is the source clock — the newest timestamp GitHub reports for
  a record. It decides *precedence*: an older payload can never overwrite newer
  state, no matter when it arrives.
- ingestion time (the event's ``received_at``) is the index clock. It decides
  *when the index learned something*, and is what fact validity windows are keyed
  on.

Without that split, a redelivery or an out-of-order webhook silently rewinds the
repository. With it, applying the same set of events in any order converges on
the same records.
"""

from __future__ import annotations

from typing import Any

from issue_graphrag.live.models import Comment, LiveState, RepoEvent, RepoItem, SourceVersion
from issue_graphrag.live.timeutil import max_iso, to_iso

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


def effective_at(payload: dict[str, Any]) -> str | None:
    """The newest moment this payload claims to describe."""
    return max_iso(
        _timestamp(payload.get("merged_at")),
        _timestamp(payload.get("closed_at")),
        _timestamp(payload.get("updated_at")),
        _timestamp(payload.get("created_at")),
    )


def _document_id(repo: str, kind: str, number: int) -> str:
    slug = "issue" if kind == "issue" else "pull"
    return f"{repo}#{slug}-{number}"


def resolve_kind(state: LiveState, repo: str, number: int, payload: dict[str, Any]) -> str:
    """Decide whether a payload describes an issue or a pull request.

    GitHub delivers pull request comments under the ``issue`` key, marked only by
    a nested ``pull_request`` object. If that marker is ever absent, an already
    known pull request still wins, so a comment cannot fork a second document.
    """
    if payload.get("pull_request") is not None or "merged" in payload or "draft" in payload:
        return "pull_request"
    if _document_id(repo, "pull_request", number) in state.items:
        return "pull_request"
    return "issue"


def merge_item(
    existing: RepoItem | None,
    repo: str,
    payload: dict[str, Any],
    kind: str,
    delivery_id: str,
    files: list[str] | None = None,
) -> RepoItem:
    """Apply each present field when its own source version wins.

    A pull request comment arrives with issue-shaped fields: no ``merged``, no
    ``merged_at``, no ``draft``. Rebuilding the record from ``payload.get(...)``
    would quietly demote a merged pull request to a plain closed one. A single
    record-wide stale guard is not enough either: two partial payloads can have
    the same timestamp and must converge whichever one arrives first. Every
    field therefore carries its own ``(effective_at, delivery_id)`` version.
    """
    number = int(payload["number"])
    item = (
        existing.model_copy(deep=True)
        if existing is not None
        else RepoItem(kind=kind, repo=repo, number=number)
    )

    incoming = SourceVersion(
        effective_at=effective_at(payload) or "",
        delivery_id=delivery_id,
    )

    def assign(field: str, value: Any) -> None:
        previous = item.field_versions.get(field)
        if previous is not None and incoming.key() < previous.key():
            return
        setattr(item, field, value)
        item.field_versions[field] = incoming.model_copy()

    item.kind = "pull_request" if kind == "pull_request" else item.kind
    item.repo = repo
    item.number = number

    if "title" in payload:
        assign("title", str(payload.get("title") or ""))
    if "body" in payload:
        assign("body", str(payload.get("body") or ""))
    if "state" in payload:
        assign("state", str(payload.get("state") or "open"))
    if "draft" in payload:
        assign("draft", bool(payload.get("draft")))
    if "merged" in payload:
        assign("merged", bool(payload.get("merged")))
    if "merged_at" in payload:
        merged_at = _timestamp(payload.get("merged_at"))
        assign("merged_at", merged_at)
        if merged_at is not None:
            assign("merged", True)
    if "labels" in payload:
        assign("labels", _labels(payload))
    if "user" in payload:
        assign("author", _login(payload))
    if payload.get("html_url"):
        assign("url", payload["html_url"])
    if "created_at" in payload:
        assign("created_at", _timestamp(payload.get("created_at")))
    if "updated_at" in payload:
        assign("updated_at", _timestamp(payload.get("updated_at")))
    if "closed_at" in payload:
        assign("closed_at", _timestamp(payload.get("closed_at")))
    if files is not None:
        assign("files", sorted(files))

    if incoming.key() >= item.version_key():
        item.effective_at = incoming.effective_at or item.effective_at
        item.source_delivery_id = delivery_id
    return item


def upsert_item(
    state: LiveState,
    repo: str,
    payload: dict[str, Any],
    kind: str,
    delivery_id: str,
    files: list[str] | None = None,
) -> tuple[str, bool]:
    """Merge a payload using independent source versions for every present field.

    Ties on ``effective_at`` are broken by delivery id. Because absent fields do
    not participate, partial payloads converge instead of whichever whole
    payload happened to arrive first winning the record.
    """
    number = int(payload["number"])
    document_id = _document_id(repo, kind, number)
    existing = state.items.get(document_id)

    before = existing.model_dump() if existing is not None else None
    merged = merge_item(existing, repo, payload, kind, delivery_id, files)
    state.items[document_id] = merged
    return document_id, before != merged.model_dump()


def upsert_comment(
    state: LiveState,
    document_id: str,
    payload: dict[str, Any],
    delivery_id: str,
) -> bool:
    """Add or edit a comment, unless it is older than what we hold or deleted."""
    item = state.items[document_id]
    comment_id = str(payload.get("id"))

    incoming = Comment(
        id=comment_id,
        author=_login(payload),
        body=str(payload.get("body") or ""),
        url=payload.get("html_url"),
        created_at=_timestamp(payload.get("created_at")),
        updated_at=_timestamp(payload.get("updated_at")),
        source_delivery_id=delivery_id,
    )

    tombstone = item.deleted_comments.get(comment_id)
    if tombstone and incoming.version_key()[0] <= tombstone.effective_at:
        # A deletion wins when GitHub gives it the same source timestamp as the
        # last edit. Delivery ids are deterministic but carry no causal order.
        return False

    existing = item.comments.get(comment_id)
    if existing is not None and incoming.version_key() < existing.version_key():
        return False

    item.comments[comment_id] = incoming
    return True


def remove_comment(
    state: LiveState,
    document_id: str,
    payload: dict[str, Any],
    deleted_at: str,
    delivery_id: str,
) -> bool:
    """Delete a comment and remember that it was deleted.

    The tombstone is what stops a late-arriving ``created`` or ``edited``
    delivery for the same comment from resurrecting it.
    """
    item = state.items[document_id]
    comment_id = str(payload.get("id"))
    deletion = SourceVersion(effective_at=deleted_at, delivery_id=delivery_id)

    previous = item.deleted_comments.get(comment_id)
    if previous is not None and deletion.key() <= previous.key():
        return False

    current = item.comments.get(comment_id)
    if current is not None and deletion.effective_at < current.version_key()[0]:
        # The delete describes an older comment version than the edit we hold.
        return False

    item.deleted_comments[comment_id] = deletion
    return item.comments.pop(comment_id, None) is not None


def apply_event_to_records(state: LiveState, event: RepoEvent) -> list[str]:
    """Update stored records from one delivery and return the documents touched.

    Only GitHub's own payload is trusted here. Nothing in this function calls an
    LLM: issue state, pull request state, labels and comment bodies are facts
    GitHub already states.
    """
    payload = event.payload
    repo = event.repo
    delivery = event.delivery_id

    if event.event_type == "issues":
        issue = payload.get("issue") or {}
        if not issue:
            raise UnsupportedEvent("issues event without an issue payload")
        number = int(issue["number"])
        kind = resolve_kind(state, repo, number, issue)
        return [upsert_item(state, repo, issue, kind, delivery)[0]]

    if event.event_type == "pull_request":
        pull = payload.get("pull_request") or {}
        if not pull:
            raise UnsupportedEvent("pull_request event without a pull_request payload")
        files = event.attachments.get("files")
        files = [str(path) for path in files] if isinstance(files, list) else None
        return [upsert_item(state, repo, pull, "pull_request", delivery, files=files)[0]]

    if event.event_type == "issue_comment":
        parent = payload.get("issue") or payload.get("pull_request") or {}
        comment = payload.get("comment") or {}
        if not parent or not comment:
            raise UnsupportedEvent("issue_comment event without an issue or comment payload")

        number = int(parent["number"])
        kind = resolve_kind(state, repo, number, parent)
        document_id, _ = upsert_item(state, repo, parent, kind, delivery)

        if event.action == "deleted":
            deleted_at = max_iso(effective_at(comment), effective_at(parent)) or event.received_at
            remove_comment(state, document_id, comment, deleted_at, delivery)
        else:
            upsert_comment(state, document_id, comment, delivery)
        return [document_id]

    raise UnsupportedEvent(f"unsupported event type: {event.event_type}")


def seed_items(repo: str, records: list[dict[str, Any]]) -> dict[str, RepoItem]:
    """Build the starting record set from a repository snapshot."""
    items: dict[str, RepoItem] = {}
    for record in records:
        item = RepoItem.model_validate({**record, "repo": record.get("repo", repo)})
        if not item.effective_at:
            item.effective_at = max_iso(
                item.merged_at, item.closed_at, item.updated_at, item.created_at
            )
        item.seed_field_versions()
        items[item.document_id] = item
    return items
