"""Scheduled GitHub snapshot reconciliation through the durable inbox.

Polling is an observation mechanism, not a second indexer.  This module keeps
conditional-request validators and a last-good source checkpoint, turns
resource diffs into deterministic ``source=reconciliation`` deliveries, and
hands those deliveries to the same single-writer lane used by webhooks.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import requests
from pydantic import BaseModel

from issue_graphrag.http_boundary import CountingSession
from issue_graphrag.ingest.github_loader import parse_repo
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.models import RepoEvent
from issue_graphrag.live.repositories import (
    canonical_repo,
    read_freshness,
    write_freshness,
)
from issue_graphrag.live.sync_checkpoint import (
    DEFAULT_CLOSED_RETENTION_SECONDS,
    DEFAULT_MAX_CHECKPOINT_BYTES,
    DEFAULT_MAX_CHECKPOINT_RESOURCES,
    DEFAULT_OPEN_RETENTION_SECONDS,
    CachedResponse,
    CheckpointPolicy,
    ParentKind,
    RepoSyncState,
    SyncResource,
    compact_sync_resources,
    read_sync_state,
    validate_checkpoint_limits,
    write_sync_state,
)
from issue_graphrag.live.timeutil import max_iso, now_utc, parse_iso, to_iso

DEFAULT_SYNC_INTERVAL_SECONDS = 15 * 60
DEFAULT_FAILURE_RETRY_SECONDS = 60
DEFAULT_API_VERSION = "2026-03-10"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SyncConfig:
    interval_seconds: int = DEFAULT_SYNC_INTERVAL_SECONDS
    failure_retry_seconds: int = DEFAULT_FAILURE_RETRY_SECONDS
    item_limit: int = 30
    item_max_pages: int = 10
    comment_limit_per_item: int = 300
    comment_max_pages: int = 10
    file_limit_per_pull: int = 3000
    file_max_pages: int = 30
    dependency_limit_per_issue: int = 1000
    dependency_max_pages: int = 10
    http_attempts: int = 3
    http_backoff_seconds: float = 1.0
    checkpoint_open_retention_seconds: int = DEFAULT_OPEN_RETENTION_SECONDS
    checkpoint_closed_retention_seconds: int = DEFAULT_CLOSED_RETENTION_SECONDS
    checkpoint_max_resources: int = DEFAULT_MAX_CHECKPOINT_RESOURCES
    checkpoint_max_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES

    def __post_init__(self) -> None:
        positive = {
            "interval_seconds": self.interval_seconds,
            "failure_retry_seconds": self.failure_retry_seconds,
            "item_limit": self.item_limit,
            "item_max_pages": self.item_max_pages,
            "comment_max_pages": self.comment_max_pages,
            "file_max_pages": self.file_max_pages,
            "dependency_max_pages": self.dependency_max_pages,
            "http_attempts": self.http_attempts,
            "checkpoint_max_resources": self.checkpoint_max_resources,
            "checkpoint_max_bytes": self.checkpoint_max_bytes,
        }
        for name, positive_value in positive.items():
            if positive_value <= 0:
                raise ValueError(f"{name} must be positive")
        non_negative: dict[str, int | float] = {
            "comment_limit_per_item": self.comment_limit_per_item,
            "file_limit_per_pull": self.file_limit_per_pull,
            "dependency_limit_per_issue": self.dependency_limit_per_issue,
            "http_backoff_seconds": self.http_backoff_seconds,
            "checkpoint_open_retention_seconds": self.checkpoint_open_retention_seconds,
            "checkpoint_closed_retention_seconds": self.checkpoint_closed_retention_seconds,
        }
        for name, non_negative_value in non_negative.items():
            if non_negative_value < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def checkpoint_policy(self) -> CheckpointPolicy:
        return CheckpointPolicy(
            open_retention_seconds=self.checkpoint_open_retention_seconds,
            closed_retention_seconds=self.checkpoint_closed_retention_seconds,
            max_resources=self.checkpoint_max_resources,
            max_bytes=self.checkpoint_max_bytes,
        )


class RateLimitedError(RuntimeError):
    """GitHub told the synchronizer when it may safely ask again."""

    def __init__(self, message: str, retry_at: str):
        super().__init__(message)
        self.retry_at = to_iso(retry_at)


@dataclass(frozen=True)
class RepositoryObservation:
    resources: dict[str, SyncResource]
    complete_comment_parents: frozenset[str]
    request_cache: dict[str, CachedResponse]
    read_requests: int
    write_requests: int
    not_modified_requests: int
    minimum_poll_interval_seconds: int


@dataclass(frozen=True)
class PaginatedResult:
    rows: tuple[dict[str, Any], ...]
    complete: bool


class RepositoryObserver(Protocol):
    def observe(
        self,
        repo: str,
        previous: RepoSyncState,
        config: SyncConfig,
        observed_at: str,
    ) -> RepositoryObservation: ...


def _header(headers: Mapping[str, Any], name: str) -> str | None:
    lowered = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == lowered:
            return str(value)
    return None


def _request_key(url: str, params: Mapping[str, Any]) -> str:
    return _sha256({"url": url, "params": dict(sorted(params.items()))})


def _retry_at_from_headers(headers: Mapping[str, Any], now: datetime) -> str:
    fallback = now + timedelta(seconds=60)
    retry_after = _header(headers, "Retry-After")
    if retry_after:
        try:
            seconds = int(retry_after)
            return to_iso(now + timedelta(seconds=seconds)) if seconds > 0 else to_iso(fallback)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                candidate = parsed.astimezone(timezone.utc)
                return to_iso(candidate if candidate > now else fallback)
            except (TypeError, ValueError):
                pass
    reset = _header(headers, "X-RateLimit-Reset")
    if reset:
        try:
            # A reset already in the past (host clock ahead of GitHub's, or a
            # stale header) must not become an immediate retry: the loop would
            # spin on refusals instead of waiting.
            candidate = datetime.fromtimestamp(int(reset), tz=timezone.utc)
            return to_iso(candidate if candidate > now else fallback)
        except (OverflowError, TypeError, ValueError):
            pass
    return to_iso(fallback)


class ConditionalGitHubClient:
    """Bounded, serial, read-only GitHub collector with durable validators."""

    def __init__(
        self,
        token: str | None = None,
        session: Any | None = None,
        timeout_seconds: float = 30.0,
        api_version: str = DEFAULT_API_VERSION,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = now_utc,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.token = token
        self.session = CountingSession(session or requests.Session())
        self.timeout_seconds = timeout_seconds
        self.api_version = api_version
        self.sleep = sleep
        self.clock = clock
        self._not_modified_requests = 0
        self._minimum_poll_interval_seconds = 0
        self._used_request_keys: set[str] = set()

    @property
    def read_request_count(self) -> int:
        return self.session.read_count

    @property
    def write_request_count(self) -> int:
        return self.session.write_count

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "github-issue-graphrag-synchronizer",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get_json(
        self,
        url: str,
        params: Mapping[str, Any],
        cache: dict[str, CachedResponse],
        config: SyncConfig,
    ) -> Any:
        key = _request_key(url, params)
        self._used_request_keys.add(key)
        prior = cache.get(key)
        headers = self._headers()
        if prior is not None and prior.etag:
            headers["If-None-Match"] = prior.etag
        elif prior is not None and prior.last_modified:
            headers["If-Modified-Since"] = prior.last_modified

        for attempt in range(1, config.http_attempts + 1):
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    params=dict(params),
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException:
                if attempt >= config.http_attempts:
                    raise
                self.sleep(config.http_backoff_seconds * (2 ** (attempt - 1)))
                continue

            response_headers = getattr(response, "headers", {}) or {}
            poll_interval = _header(response_headers, "X-Poll-Interval")
            if poll_interval:
                try:
                    self._minimum_poll_interval_seconds = max(
                        self._minimum_poll_interval_seconds,
                        max(0, int(poll_interval)),
                    )
                except ValueError:
                    pass

            status = int(response.status_code)
            if status == 304:
                if prior is None:
                    raise ValueError("GitHub returned 304 without a cached representation")
                self._not_modified_requests += 1
                return prior.payload

            remaining = _header(response_headers, "X-RateLimit-Remaining")
            retry_after = _header(response_headers, "Retry-After")
            if status == 429 or (status == 403 and (remaining == "0" or retry_after)):
                retry_at = _retry_at_from_headers(response_headers, self.clock())
                raise RateLimitedError("GitHub API rate limit exhausted", retry_at)

            if status >= 500 and attempt < config.http_attempts:
                self.sleep(config.http_backoff_seconds * (2 ** (attempt - 1)))
                continue

            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, (dict, list)):
                raise ValueError(f"GitHub returned unsupported JSON for {url}")
            cache[key] = CachedResponse(
                etag=_header(response_headers, "ETag"),
                last_modified=_header(response_headers, "Last-Modified"),
                payload=payload,
            )
            return payload

        raise RuntimeError("unreachable HTTP retry state")

    def _get_object(
        self,
        url: str,
        cache: dict[str, CachedResponse],
        config: SyncConfig,
    ) -> dict[str, Any]:
        payload = self._get_json(url, {}, cache, config)
        if not isinstance(payload, dict):
            raise ValueError(f"GitHub returned a non-object payload for {url}")
        return payload

    def _get_paginated(
        self,
        url: str,
        *,
        params: Mapping[str, Any],
        limit: int,
        max_pages: int,
        cache: dict[str, CachedResponse],
        config: SyncConfig,
    ) -> PaginatedResult:
        if limit == 0:
            return PaginatedResult(rows=(), complete=False)
        rows: list[dict[str, Any]] = []
        complete = False
        for page in range(1, max_pages + 1):
            remaining = limit - len(rows)
            if remaining <= 0:
                break
            per_page = min(100, remaining)
            page_payload = self._get_json(
                url,
                {**params, "per_page": per_page, "page": page},
                cache,
                config,
            )
            if not isinstance(page_payload, list):
                raise ValueError(f"GitHub returned a non-list payload for {url}")
            page_rows = [row for row in page_payload if isinstance(row, dict)]
            rows.extend(page_rows[:remaining])
            if len(page_payload) < per_page:
                complete = True
                break
        return PaginatedResult(rows=tuple(rows), complete=complete)

    def observe(
        self,
        repo: str,
        previous: RepoSyncState,
        config: SyncConfig,
        observed_at: str,
    ) -> RepositoryObservation:
        normalized = canonical_repo(repo)
        owner, name = parse_repo(normalized)
        cache = {key: value.model_copy(deep=True) for key, value in previous.request_cache.items()}
        start_reads = self.read_request_count
        start_writes = self.write_request_count
        start_not_modified = self._not_modified_requests
        self._minimum_poll_interval_seconds = 0
        self._used_request_keys = set()

        api = f"https://api.github.com/repos/{owner}/{name}"
        listed = self._get_paginated(
            f"{api}/issues",
            params={"state": "all", "sort": "updated", "direction": "desc"},
            limit=config.item_limit,
            max_pages=config.item_max_pages,
            cache=cache,
            config=config,
        )

        resources: dict[str, SyncResource] = {}
        complete_comment_parents: set[str] = set()
        seen_numbers: set[int] = set()
        for listed_item in listed.rows:
            if listed_item.get("number") is None:
                raise ValueError("GitHub issue list row is missing number")
            number = int(listed_item["number"])
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
            is_pull = "pull_request" in listed_item
            kind: ParentKind = "pull_request" if is_pull else "issue"
            raw = dict(listed_item)
            # A window this poll could not read to the end is not an
            # observation. Attaching a truncated file set would state that the
            # pull request touches fewer modules than it does; leaving the
            # attachment out keeps the last-good value and lets the worker's
            # own bounded hydration fill it in.
            files: list[str] | None = None
            if is_pull:
                raw.update(self._get_object(f"{api}/pulls/{number}", cache, config))
                file_rows = self._get_paginated(
                    f"{api}/pulls/{number}/files",
                    params={},
                    limit=config.file_limit_per_pull,
                    max_pages=config.file_max_pages,
                    cache=cache,
                    config=config,
                )
                if file_rows.complete:
                    files = sorted(
                        {str(row["filename"]) for row in file_rows.rows if row.get("filename")}
                    )

            comments = self._get_paginated(
                f"{api}/issues/{number}/comments",
                params={"sort": "updated", "direction": "desc"},
                limit=config.comment_limit_per_item,
                max_pages=config.comment_max_pages,
                cache=cache,
                config=config,
            )
            # Same rule, and it matters more here: a partially read blocked_by
            # page counted as a number would settle an unknown count at zero and
            # publish a blocked issue as available.
            dependency_count: int | None = None
            if kind == "issue":
                blockers = self._get_paginated(
                    f"{api}/issues/{number}/dependencies/blocked_by",
                    params={},
                    limit=config.dependency_limit_per_issue,
                    max_pages=config.dependency_max_pages,
                    cache=cache,
                    config=config,
                )
                if blockers.complete:
                    dependency_count = sum(
                        str(row.get("state") or "").casefold() == "open" for row in blockers.rows
                    )

            item_payload = _item_payload(raw, kind)
            source_updated_at = _item_updated_at(item_payload, observed_at)
            item_key = f"{kind}:{number}"
            if comments.complete:
                complete_comment_parents.add(item_key)
            resources[item_key] = SyncResource.observed(
                kind=kind,
                identity=item_key,
                source_updated_at=source_updated_at,
                last_observed_at=observed_at,
                payload=item_payload,
                attachments={"files": files} if files is not None else {},
                parent_kind=kind,
                parent_number=number,
            )

            if kind == "issue" and dependency_count is not None:
                dependency_key = f"dependency:{number}"
                resources[dependency_key] = SyncResource.observed(
                    kind="dependency",
                    identity=dependency_key,
                    source_updated_at=source_updated_at,
                    last_observed_at=observed_at,
                    payload={"count": dependency_count},
                    attachments={"blocking_dependency_count": dependency_count},
                    parent_kind=kind,
                    parent_number=number,
                )

            comment_ids: list[str] = []
            for raw_comment in comments.rows:
                if raw_comment.get("id") is None:
                    raise ValueError("GitHub issue comment is missing id")
                comment = _comment_payload(raw_comment)
                comment_id = str(comment["id"])
                comment_ids.append(comment_id)
                comment_key = f"comment:{comment_id}"
                resources[comment_key] = SyncResource.observed(
                    kind="comment",
                    identity=comment_key,
                    source_updated_at=(
                        comment.get("updated_at") or comment.get("created_at") or source_updated_at
                    ),
                    last_observed_at=observed_at,
                    payload=comment,
                    parent_kind=kind,
                    parent_number=number,
                )

            if comments.complete:
                manifest_key = f"comment_manifest:{kind}:{number}"
                resources[manifest_key] = SyncResource.observed(
                    kind="comment_manifest",
                    identity=manifest_key,
                    # This is an absence observation, so the poll clock is its
                    # source-side effective time. The manifest fingerprint is
                    # only the stable complete id set; unchanged polls remain
                    # no-ops even though their observation clock advances.
                    source_updated_at=observed_at,
                    last_observed_at=observed_at,
                    payload={},
                    attachments={"comment_ids": sorted(comment_ids)},
                    parent_kind=kind,
                    parent_number=number,
                )

        return RepositoryObservation(
            resources=resources,
            complete_comment_parents=frozenset(complete_comment_parents),
            request_cache={
                key: cache[key] for key in sorted(self._used_request_keys) if key in cache
            },
            read_requests=self.read_request_count - start_reads,
            write_requests=self.write_request_count - start_writes,
            not_modified_requests=self._not_modified_requests - start_not_modified,
            minimum_poll_interval_seconds=self._minimum_poll_interval_seconds,
        )


def _item_payload(raw: Mapping[str, Any], kind: ParentKind) -> dict[str, Any]:
    labels = sorted(
        {
            str(label.get("name") or "")
            for label in raw.get("labels") or []
            if isinstance(label, dict) and label.get("name")
        }
    )
    assignees = sorted(
        {
            str(assignee.get("login") or "")
            for assignee in raw.get("assignees") or []
            if isinstance(assignee, dict) and assignee.get("login")
        }
    )
    user = raw.get("user") or {}
    payload: dict[str, Any] = {
        "number": int(raw["number"]),
        "title": str(raw.get("title") or ""),
        "body": str(raw.get("body") or ""),
        "state": str(raw.get("state") or "open"),
        "labels": [{"name": name} for name in labels],
        "assignees": [{"login": login} for login in assignees],
        "locked": bool(raw.get("locked")),
        "user": {"login": str(user.get("login") or "")}
        if isinstance(user, dict)
        else {"login": ""},
        "html_url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "closed_at": raw.get("closed_at"),
    }
    if kind == "pull_request":
        payload.update(
            {
                "draft": bool(raw.get("draft")),
                "merged": bool(raw.get("merged")) or raw.get("merged_at") is not None,
                "merged_at": raw.get("merged_at"),
            }
        )
    return payload


def _comment_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    user = raw.get("user") or {}
    return {
        "id": str(raw["id"]),
        "body": str(raw.get("body") or ""),
        "user": {"login": str(user.get("login") or "")}
        if isinstance(user, dict)
        else {"login": ""},
        "html_url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


def _item_updated_at(payload: Mapping[str, Any], fallback: str) -> str:
    return max_iso(
        payload.get("merged_at"),
        payload.get("closed_at"),
        payload.get("updated_at"),
        payload.get("created_at"),
    ) or to_iso(fallback)


def reconciliation_delivery_id(
    repo: str,
    resource_identity: str,
    source_updated_at: str,
    content_fingerprint: str,
) -> str:
    """Stable synthetic identity required for retry-safe scheduled polling."""
    digest = _sha256(
        {
            "repo": canonical_repo(repo),
            "resource_identity": resource_identity,
            "source_updated_at": to_iso(source_updated_at),
            "content_fingerprint": content_fingerprint,
        }
    )
    return f"reconciliation-{digest}"


def _comment_manifest_delivery_id(
    repo: str,
    resource: SyncResource,
    previous: SyncResource | None,
) -> str:
    """Identify one durable manifest transition, independently of retry time.

    Content identity alone is insufficient: a set can return to an earlier
    value (``[a] -> [a, b] -> [a]``), but the final transition must still be
    delivered.  The previous checkpoint generation distinguishes later state
    transitions while remaining stable across a retry that did not advance
    the checkpoint.
    """
    digest = _sha256(
        {
            "repo": canonical_repo(repo),
            "resource_identity": resource.identity,
            "previous_fingerprint": previous.fingerprint if previous else None,
            "previous_observed_at": previous.last_observed_at if previous else None,
            "content_fingerprint": resource.fingerprint,
        }
    )
    return f"reconciliation-{digest}"


def _event_for_resource(
    repo: str,
    resource: SyncResource,
    previous: SyncResource | None,
) -> RepoEvent:
    delivery_id = (
        _comment_manifest_delivery_id(repo, resource, previous)
        if resource.kind == "comment_manifest"
        else reconciliation_delivery_id(
            repo,
            resource.identity,
            resource.source_updated_at,
            resource.fingerprint,
        )
    )
    repository = {"full_name": repo}
    if resource.kind == "issue":
        payload = {
            "action": "reconciled",
            "repository": repository,
            "issue": resource.payload,
        }
        event_type = "issues"
    elif resource.kind == "pull_request":
        payload = {
            "action": "reconciled",
            "repository": repository,
            "pull_request": resource.payload,
        }
        event_type = "pull_request"
    elif resource.kind == "comment":
        parent: dict[str, Any] = {"number": resource.parent_number}
        if resource.parent_kind == "pull_request":
            parent["pull_request"] = {}
        payload = {
            "action": "created" if previous is None else "edited",
            "repository": repository,
            "issue": parent,
            "comment": resource.payload,
        }
        event_type = "issue_comment"
    elif resource.kind == "comment_manifest":
        parent = {"number": resource.parent_number}
        if resource.parent_kind == "pull_request":
            parent["pull_request"] = {}
        payload = {
            "action": "reconciled",
            "repository": repository,
            "issue": parent,
        }
        event_type = "issue_comments"
    elif resource.kind == "dependency":
        payload = {
            "action": "reconciled",
            "repository": repository,
            "blocked_issue": {
                "number": resource.parent_number,
                "repository_url": f"https://api.github.com/repos/{repo}",
            },
        }
        event_type = "issue_dependencies"
    else:  # pragma: no cover - Pydantic keeps this unreachable.
        raise ValueError(f"unsupported sync resource kind: {resource.kind}")

    return RepoEvent(
        delivery_id=delivery_id,
        event_type=event_type,
        action=str(payload["action"]),
        repo=repo,
        received_at=resource.source_updated_at,
        payload=payload,
        attachments=resource.attachments,
        source="reconciliation",
    )


def _deleted_comment_event(
    repo: str,
    resource: SyncResource,
    source_updated_at: str,
) -> RepoEvent:
    fingerprint = _sha256(
        {
            "deleted": resource.payload,
            "parent_kind": resource.parent_kind,
            "parent_number": resource.parent_number,
        }
    )
    delivery_id = reconciliation_delivery_id(
        repo,
        resource.identity,
        source_updated_at,
        fingerprint,
    )
    parent: dict[str, Any] = {"number": resource.parent_number}
    if resource.parent_kind == "pull_request":
        parent["pull_request"] = {}
    return RepoEvent(
        delivery_id=delivery_id,
        event_type="issue_comment",
        action="deleted",
        repo=repo,
        received_at=source_updated_at,
        payload={
            "action": "deleted",
            "repository": {"full_name": repo},
            "issue": parent,
            "comment": resource.payload,
        },
        source="reconciliation",
    )


@dataclass(frozen=True)
class ReconciliationPlan:
    events: tuple[RepoEvent, ...]
    resources: dict[str, SyncResource]


def plan_reconciliation(
    repo: str,
    previous: RepoSyncState,
    observation: RepositoryObservation,
) -> ReconciliationPlan:
    """Diff only resources in the bounded window observed by this poll.

    Absence outside that window says nothing, so older unobserved items remain
    in the checkpoint.  Within an observed parent, however, the comment list is
    complete up to the configured cap and can surface deletions.
    """
    normalized = canonical_repo(repo)
    resources = {key: value.model_copy(deep=True) for key, value in previous.resources.items()}
    events: list[RepoEvent] = []

    current_comment_keys = {
        key for key, resource in observation.resources.items() if resource.kind == "comment"
    }
    current_manifests = {
        f"{resource.parent_kind}:{resource.parent_number}": resource
        for resource in observation.resources.values()
        if resource.kind == "comment_manifest"
        and resource.parent_kind is not None
        and resource.parent_number is not None
    }
    for key, old in sorted(previous.resources.items()):
        parent_key = (
            f"{old.parent_kind}:{old.parent_number}"
            if old.parent_kind is not None and old.parent_number is not None
            else None
        )
        if (
            old.kind == "comment"
            and parent_key in observation.complete_comment_parents
            and key not in current_comment_keys
        ):
            manifest = current_manifests.get(parent_key or "")
            if manifest is None:
                # Compatibility for a synthetic/legacy observer that proves a
                # complete window but does not yet publish a manifest.
                current_parent = observation.resources.get(parent_key or "")
                source_updated_at = (
                    current_parent.source_updated_at
                    if current_parent is not None
                    else old.source_updated_at
                )
                events.append(_deleted_comment_event(normalized, old, source_updated_at))
            resources.pop(key, None)

    for key, current in sorted(observation.resources.items()):
        prior = previous.resources.get(key)
        changed = prior is None or prior.fingerprint != current.fingerprint
        if changed:
            # Including a first fully observed zero dependency count. The
            # checkpoint says nothing about live state, so suppressing that
            # baseline is what would keep a stale blocking count alive after a
            # missed blocked_by_removed webhook - the drift this lane exists to
            # repair. Every other resource kind already emits its baseline.
            events.append(_event_for_resource(normalized, current, prior))
        resources[key] = current.model_copy(deep=True)

    # Keep plan output stable and place the aggregate observation last for
    # human inspection. Correctness does not depend on inbox order: a manifest
    # never removes an id it contains, and source clocks protect newer edits.
    events.sort(
        key=lambda event: (
            event.received_at,
            event.event_type == "issue_comments",
            event.delivery_id,
        )
    )
    return ReconciliationPlan(events=tuple(events), resources=resources)


class SyncResult(BaseModel):
    repo: str
    status: Literal["succeeded", "rate_limited", "failed"]
    attempted_at: str
    next_sync_at: str
    observed_resources: int = 0
    planned_deliveries: int = 0
    enqueued: int = 0
    duplicates: int = 0
    requeued: int = 0
    read_requests: int = 0
    not_modified_requests: int = 0
    write_requests: int = 0
    checkpoint_resources: int = 0
    checkpoint_bytes: int = 0
    compacted_resources: int = 0
    compacted_families: int = 0
    error: str | None = None


class ScheduledSynchronizer:
    """One deterministic poll attempt for one repository-qualified lane."""

    def __init__(
        self,
        *,
        repo: str,
        inbox: DeliveryInbox,
        sync_state_path: Path,
        freshness_path: Path,
        observer: RepositoryObserver,
        config: SyncConfig | None = None,
    ):
        self.repo = canonical_repo(repo)
        self.inbox = inbox
        self.sync_state_path = Path(sync_state_path)
        self.freshness_path = Path(freshness_path)
        self.observer = observer
        self.config = config or SyncConfig()

    def _freshness_failure(self, attempted_at: str, next_sync_at: str, error: str) -> None:
        freshness = read_freshness(self.freshness_path, self.repo)
        freshness.last_source_attempt_at = attempted_at
        freshness.next_source_sync_at = next_sync_at
        freshness.source_status = "stale"
        freshness.source_kind = "scheduled_sync"
        freshness.source_error = error[:4000]
        freshness.sync_interval_seconds = self.config.interval_seconds
        write_freshness(self.freshness_path, freshness)

    def sync_once(self, now: str | None = None) -> SyncResult:
        attempted_at = to_iso(now) if now else to_iso(now_utc())
        observation: RepositoryObservation | None = None
        checkpoint_resources = 0
        checkpoint_bytes = 0
        compacted_resources = 0
        compacted_families = 0
        try:
            previous = read_sync_state(self.sync_state_path, self.repo)
            observation = self.observer.observe(
                self.repo,
                previous,
                self.config,
                attempted_at,
            )
            if observation.write_requests:
                raise RuntimeError(
                    f"scheduled synchronizer attempted a GitHub write: {observation.write_requests}"
                )
            plan = plan_reconciliation(self.repo, previous, observation)
            for key in observation.resources:
                plan.resources[key].last_observed_at = attempted_at
            compaction = compact_sync_resources(
                plan.resources,
                observed_identities=frozenset(observation.resources),
                compacted_at=attempted_at,
                policy=self.config.checkpoint_policy,
            )
            compacted_resources = compaction.compacted_resources
            compacted_families = compaction.compacted_families
            next_state = RepoSyncState(
                repo=self.repo,
                last_observed_at=attempted_at,
                last_compacted_at=(
                    attempted_at if compacted_families else previous.last_compacted_at
                ),
                compacted_resources_total=(
                    previous.compacted_resources_total + compacted_resources
                ),
                compacted_families_total=(previous.compacted_families_total + compacted_families),
                request_cache=observation.request_cache,
                resources=compaction.resources,
            )
            checkpoint_resources = len(next_state.resources)
            # Check before enqueue so an operator ceiling cannot create a new
            # batch that the synchronizer already knows it cannot checkpoint.
            checkpoint_bytes = validate_checkpoint_limits(
                next_state,
                self.config.checkpoint_policy,
            )
            outcomes = [self.inbox.enqueue(event, attempted_at) for event in plan.events]
            effective_interval = max(
                self.config.interval_seconds,
                observation.minimum_poll_interval_seconds,
            )
            next_sync_at = to_iso(parse_iso(attempted_at) + timedelta(seconds=effective_interval))
            write_sync_state(
                self.sync_state_path,
                next_state,
                self.config.checkpoint_policy,
            )
            freshness = read_freshness(self.freshness_path, self.repo)
            freshness.last_source_attempt_at = attempted_at
            freshness.last_source_sync_at = attempted_at
            freshness.next_source_sync_at = next_sync_at
            freshness.source_status = "current"
            freshness.source_kind = "scheduled_sync"
            freshness.source_error = None
            freshness.sync_interval_seconds = effective_interval
            freshness.last_source_requests = observation.read_requests
            freshness.last_source_not_modified = observation.not_modified_requests
            freshness.last_source_deliveries = len(plan.events)
            write_freshness(self.freshness_path, freshness)
            return SyncResult(
                repo=self.repo,
                status="succeeded",
                attempted_at=attempted_at,
                next_sync_at=next_sync_at,
                observed_resources=len(observation.resources),
                planned_deliveries=len(plan.events),
                enqueued=sum(outcome.outcome == "enqueued" for outcome in outcomes),
                duplicates=sum(outcome.outcome == "duplicate" for outcome in outcomes),
                requeued=sum(outcome.outcome == "requeued" for outcome in outcomes),
                read_requests=observation.read_requests,
                not_modified_requests=observation.not_modified_requests,
                write_requests=observation.write_requests,
                checkpoint_resources=checkpoint_resources,
                checkpoint_bytes=checkpoint_bytes,
                compacted_resources=compacted_resources,
                compacted_families=compacted_families,
            )
        except RateLimitedError as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._freshness_failure(attempted_at, exc.retry_at, error)
            return SyncResult(
                repo=self.repo,
                status="rate_limited",
                attempted_at=attempted_at,
                next_sync_at=exc.retry_at,
                error=error,
            )
        except Exception as exc:
            next_sync_at = to_iso(
                parse_iso(attempted_at) + timedelta(seconds=self.config.failure_retry_seconds)
            )
            error = f"{type(exc).__name__}: {exc}"
            self._freshness_failure(attempted_at, next_sync_at, error)
            return SyncResult(
                repo=self.repo,
                status="failed",
                attempted_at=attempted_at,
                next_sync_at=next_sync_at,
                observed_resources=(len(observation.resources) if observation else 0),
                read_requests=(observation.read_requests if observation else 0),
                not_modified_requests=(observation.not_modified_requests if observation else 0),
                write_requests=(observation.write_requests if observation else 0),
                checkpoint_resources=checkpoint_resources,
                checkpoint_bytes=checkpoint_bytes,
                compacted_resources=compacted_resources,
                compacted_families=compacted_families,
                error=error,
            )


def run_synchronizer_loop(
    synchronizer: ScheduledSynchronizer,
    on_result: Callable[[SyncResult], None],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = now_utc,
    should_stop: Callable[[], bool] = lambda: False,
    wait: Callable[[float], object] | None = None,
) -> None:
    """Run fixed-schedule polls while respecting a rate-limit retry timestamp."""
    while True:
        if should_stop():
            return
        result = synchronizer.sync_once()
        on_result(result)
        remaining = (parse_iso(result.next_sync_at) - clock()).total_seconds()
        delay = max(0.0, remaining)
        wait(delay) if wait is not None else sleep(delay)
