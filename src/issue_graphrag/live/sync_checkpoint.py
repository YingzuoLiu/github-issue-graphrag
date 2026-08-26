"""Bounded, recoverable scheduled-sync checkpoints.

The checkpoint is an observation cache, not the source of product truth.  It
must nevertheless survive restarts and remain trustworthy because scheduled
reconciliation uses it to decide which deterministic deliveries to enqueue.

Version 2 adds three operational properties:

* resource families carry a last-observed clock and can be compacted as one
  unit after a state-sensitive retention period;
* a hard resource/byte ceiling fails closed before an unbounded checkpoint is
  committed;
* every commit keeps a verified last-good copy, while recovery is an explicit,
  confirmed and audited operator action.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

from issue_graphrag.live.repositories import canonical_repo
from issue_graphrag.live.timeutil import max_iso, now_utc, parse_iso, to_iso

LEGACY_SYNC_STATE_VERSION = 1
SYNC_STATE_VERSION = 2

DEFAULT_OPEN_RETENTION_SECONDS = 90 * 24 * 60 * 60
DEFAULT_CLOSED_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MAX_CHECKPOINT_RESOURCES = 12_000
DEFAULT_MAX_CHECKPOINT_BYTES = 64 * 1024 * 1024

ResourceKind = Literal[
    "issue",
    "pull_request",
    "comment",
    "dependency",
    "comment_manifest",
]
ParentKind = Literal["issue", "pull_request"]
CheckpointState = Literal["missing", "healthy", "corrupt"]
RecoveryAction = Literal["restore_last_good", "rebaseline"]


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class CachedResponse(BaseModel):
    """One reusable representation plus GitHub's validators for its exact request."""

    etag: str | None = None
    last_modified: str | None = None
    payload: Any


class SyncResource(BaseModel):
    """Canonical current-state resource retained for deterministic diffing."""

    kind: ResourceKind
    identity: str
    source_updated_at: str
    last_observed_at: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    attachments: dict[str, Any] = Field(default_factory=dict)
    parent_kind: ParentKind | None = None
    parent_number: int | None = None
    fingerprint: str

    @classmethod
    def observed(
        cls,
        *,
        kind: ResourceKind,
        identity: str,
        source_updated_at: str,
        payload: Mapping[str, Any],
        attachments: Mapping[str, Any] | None = None,
        parent_kind: ParentKind | None = None,
        parent_number: int | None = None,
        last_observed_at: str | None = None,
    ) -> "SyncResource":
        payload_dict = dict(payload)
        attachment_dict = dict(attachments or {})
        content = {
            "payload": payload_dict,
            "attachments": attachment_dict,
            "parent_kind": parent_kind,
            "parent_number": parent_number,
        }
        return cls(
            kind=kind,
            identity=identity,
            source_updated_at=to_iso(source_updated_at),
            last_observed_at=to_iso(last_observed_at) if last_observed_at else None,
            payload=payload_dict,
            attachments=attachment_dict,
            parent_kind=parent_kind,
            parent_number=parent_number,
            fingerprint=_sha256(content),
        )

    def validate_fingerprint(self) -> None:
        expected = _sha256(
            {
                "payload": self.payload,
                "attachments": self.attachments,
                "parent_kind": self.parent_kind,
                "parent_number": self.parent_number,
            }
        )
        if expected != self.fingerprint:
            raise ValueError(f"sync resource fingerprint mismatch: {self.identity}")


class RepoSyncState(BaseModel):
    """Last-good observation and conditional HTTP cache for one repository lane."""

    version: int = SYNC_STATE_VERSION
    repo: str
    last_observed_at: str | None = None
    last_compacted_at: str | None = None
    compacted_resources_total: int = Field(default=0, ge=0)
    compacted_families_total: int = Field(default=0, ge=0)
    request_cache: dict[str, CachedResponse] = Field(default_factory=dict)
    resources: dict[str, SyncResource] = Field(default_factory=dict)


@dataclass(frozen=True)
class CheckpointPolicy:
    """Deterministic retention and hard-stop limits for one checkpoint."""

    open_retention_seconds: int = DEFAULT_OPEN_RETENTION_SECONDS
    closed_retention_seconds: int = DEFAULT_CLOSED_RETENTION_SECONDS
    max_resources: int = DEFAULT_MAX_CHECKPOINT_RESOURCES
    max_bytes: int = DEFAULT_MAX_CHECKPOINT_BYTES

    def __post_init__(self) -> None:
        for name, value in {
            "open_retention_seconds": self.open_retention_seconds,
            "closed_retention_seconds": self.closed_retention_seconds,
        }.items():
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        for name, value in {
            "max_resources": self.max_resources,
            "max_bytes": self.max_bytes,
        }.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")


class SyncCheckpointError(RuntimeError):
    """A checkpoint cannot be trusted or safely replaced."""


class CheckpointLimitExceeded(SyncCheckpointError):
    """The next checkpoint would cross an operator-visible hard ceiling."""


class RecoveryConfirmationError(SyncCheckpointError):
    """A mutating recovery was attempted without the exact repository confirmation."""


@dataclass(frozen=True)
class SyncCheckpointPaths:
    primary: Path
    last_good: Path
    quarantine: Path
    recovery: Path


def sync_checkpoint_paths(path: Path) -> SyncCheckpointPaths:
    primary = Path(path)
    return SyncCheckpointPaths(
        primary=primary,
        last_good=primary.with_name(f"{primary.stem}.last-good{primary.suffix}"),
        quarantine=primary.with_name(f"{primary.stem}.quarantine"),
        recovery=primary.with_name(f"{primary.stem}.recovery"),
    )


def _validate_state(state: RepoSyncState, repo: str) -> RepoSyncState:
    normalized = canonical_repo(repo)
    if state.version != SYNC_STATE_VERSION:
        raise ValueError(f"unsupported sync state version: {state.version}")
    if canonical_repo(state.repo) != normalized:
        raise ValueError(f"sync state belongs to {state.repo!r}, not {normalized!r}")
    state.repo = normalized
    if state.last_observed_at:
        state.last_observed_at = to_iso(state.last_observed_at)
    if state.last_compacted_at:
        state.last_compacted_at = to_iso(state.last_compacted_at)
    for key, resource in state.resources.items():
        if key != resource.identity:
            raise ValueError(f"sync resource key does not match identity: {key!r}")
        resource.source_updated_at = to_iso(resource.source_updated_at)
        if resource.last_observed_at:
            resource.last_observed_at = to_iso(resource.last_observed_at)
        resource.validate_fingerprint()
    return state


def _decode_sync_state(data: bytes, repo: str) -> tuple[RepoSyncState, int]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid checkpoint JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("sync checkpoint must contain a JSON object")
    raw_version = int(payload.get("version", LEGACY_SYNC_STATE_VERSION))
    if raw_version not in {LEGACY_SYNC_STATE_VERSION, SYNC_STATE_VERSION}:
        raise ValueError(f"unsupported sync state version: {raw_version}")
    state = RepoSyncState.model_validate(payload)
    if raw_version == LEGACY_SYNC_STATE_VERSION:
        migrated_seen_at = state.last_observed_at
        for resource in state.resources.values():
            if resource.last_observed_at is None:
                resource.last_observed_at = migrated_seen_at or resource.source_updated_at
        state.version = SYNC_STATE_VERSION
    return _validate_state(state, repo), raw_version


def read_sync_state(path: Path, repo: str) -> RepoSyncState:
    normalized = canonical_repo(repo)
    checkpoint = Path(path)
    if not checkpoint.exists():
        if sync_checkpoint_paths(checkpoint).last_good.exists():
            raise SyncCheckpointError(
                "primary checkpoint is missing while last-good exists; "
                "use explicit recovery or rebaseline"
            )
        return RepoSyncState(repo=normalized)
    try:
        state, _ = _decode_sync_state(checkpoint.read_bytes(), normalized)
        return state
    except Exception as exc:
        if isinstance(exc, SyncCheckpointError):
            raise
        raise SyncCheckpointError(f"checkpoint {checkpoint} is corrupt: {exc}") from exc


def _checkpoint_bytes(state: RepoSyncState) -> bytes:
    payload = state.model_dump(mode="json")
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def checkpoint_size_bytes(state: RepoSyncState) -> int:
    candidate = state.model_copy(deep=True)
    candidate.version = SYNC_STATE_VERSION
    _validate_state(candidate, candidate.repo)
    return len(_checkpoint_bytes(candidate))


def validate_checkpoint_limits(
    state: RepoSyncState,
    policy: CheckpointPolicy,
) -> int:
    resource_count = len(state.resources)
    if resource_count > policy.max_resources:
        raise CheckpointLimitExceeded(
            "checkpoint resource limit exceeded: "
            f"{resource_count} > {policy.max_resources}; last-good checkpoint preserved"
        )
    size = checkpoint_size_bytes(state)
    if size > policy.max_bytes:
        raise CheckpointLimitExceeded(
            "checkpoint byte limit exceeded: "
            f"{size} > {policy.max_bytes}; last-good checkpoint preserved"
        )
    return size


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":  # Directory handles are not portable on Windows.
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_verified_checkpoint_copy(path: Path, payload: bytes, repo: str) -> None:
    _atomic_write_bytes(path, payload)
    try:
        _decode_sync_state(path.read_bytes(), repo)
    except Exception as exc:
        raise SyncCheckpointError(f"checkpoint copy failed verification: {path}") from exc


def write_sync_state(
    path: Path,
    state: RepoSyncState,
    policy: CheckpointPolicy | None = None,
) -> None:
    """Commit v2 state while retaining a separately verified previous generation."""
    checkpoint = Path(path)
    paths = sync_checkpoint_paths(checkpoint)
    candidate = state.model_copy(deep=True)
    candidate.version = SYNC_STATE_VERSION
    candidate.repo = canonical_repo(candidate.repo)
    _validate_state(candidate, candidate.repo)
    candidate_bytes = _checkpoint_bytes(candidate)
    validate_checkpoint_limits(candidate, policy or CheckpointPolicy())

    if checkpoint.exists():
        current_bytes = checkpoint.read_bytes()
        try:
            _decode_sync_state(current_bytes, candidate.repo)
        except Exception as exc:
            raise SyncCheckpointError(
                "refusing to overwrite a corrupt primary checkpoint; "
                "use explicit recovery or rebaseline"
            ) from exc
        _write_verified_checkpoint_copy(paths.last_good, current_bytes, candidate.repo)
    else:
        if paths.last_good.exists():
            raise SyncCheckpointError(
                "refusing to replace a missing primary while last-good exists; "
                "use explicit recovery or rebaseline"
            )
        # Seed the recoverable generation first. A crash before the primary
        # replace then leaves a healthy last-good file rather than no recovery
        # path at all.
        _write_verified_checkpoint_copy(paths.last_good, candidate_bytes, candidate.repo)

    _atomic_write_bytes(checkpoint, candidate_bytes)


def resource_family(resource: SyncResource) -> str:
    if resource.parent_kind is not None and resource.parent_number is not None:
        return f"{resource.parent_kind}:{resource.parent_number}"
    return resource.identity


def _family_is_closed(resources: list[SyncResource]) -> bool:
    for resource in resources:
        if resource.kind not in {"issue", "pull_request"}:
            continue
        state = str(resource.payload.get("state") or "open").casefold()
        if state == "closed" or bool(resource.payload.get("merged")):
            return True
    return False


@dataclass(frozen=True)
class CompactionResult:
    resources: dict[str, SyncResource]
    compacted_families: int
    compacted_resources: int


def compact_sync_resources(
    resources: Mapping[str, SyncResource],
    *,
    observed_identities: set[str] | frozenset[str],
    compacted_at: str,
    policy: CheckpointPolicy,
) -> CompactionResult:
    """Drop only whole, inactive families whose state-specific retention elapsed.

    A family is the parent item plus its dependency, comments and complete
    comment manifest.  Grouping prevents an incomplete comment window from
    evicting only the older comments needed by the next complete observation.
    """
    now = parse_iso(compacted_at)
    copied = {key: value.model_copy(deep=True) for key, value in resources.items()}
    families: dict[str, list[tuple[str, SyncResource]]] = {}
    for key, resource in copied.items():
        families.setdefault(resource_family(resource), []).append((key, resource))

    removed_families = 0
    removed_resources = 0
    for rows in families.values():
        keys = {key for key, _ in rows}
        if keys & set(observed_identities):
            continue
        family_resources = [resource for _, resource in rows]
        last_seen = max_iso(
            *(
                resource.last_observed_at or resource.source_updated_at
                for resource in family_resources
            )
        )
        if last_seen is None:
            continue
        retention = (
            policy.closed_retention_seconds
            if _family_is_closed(family_resources)
            else policy.open_retention_seconds
        )
        eligible_at = parse_iso(last_seen) + timedelta(seconds=retention)
        if now < eligible_at:
            continue
        for key in keys:
            copied.pop(key, None)
        removed_families += 1
        removed_resources += len(keys)

    return CompactionResult(
        resources=copied,
        compacted_families=removed_families,
        compacted_resources=removed_resources,
    )


class CheckpointInspection(BaseModel):
    repo: str
    path: str
    state: CheckpointState
    on_disk_version: int | None = None
    bytes: int = 0
    resources: int = 0
    families: int = 0
    resource_kinds: dict[str, int] = Field(default_factory=dict)
    conditional_responses: int = 0
    last_observed_at: str | None = None
    last_compacted_at: str | None = None
    compacted_resources_total: int = 0
    compacted_families_total: int = 0
    max_resources: int
    max_bytes: int
    last_good_state: CheckpointState
    last_good_bytes: int = 0
    last_good_error: str | None = None
    quarantine_files: int = 0
    recovery_records: int = 0
    pending_recoveries: int = 0
    latest_recovery_status: str | None = None
    latest_recovery_at: str | None = None
    error: str | None = None


def _inspect_file(
    path: Path, repo: str
) -> tuple[CheckpointState, RepoSyncState | None, int | None, str | None]:
    if not path.exists():
        return "missing", None, None, None
    try:
        state, version = _decode_sync_state(path.read_bytes(), repo)
        return "healthy", state, version, None
    except Exception as exc:
        return "corrupt", None, None, f"{type(exc).__name__}: {exc}"


def _recovery_metadata(directory: Path) -> tuple[int, int, str | None, str | None]:
    if not directory.exists():
        return 0, 0, None, None
    records = sorted(candidate for candidate in directory.iterdir() if candidate.is_file())
    pending = 0
    latest_status: str | None = None
    latest_at: str | None = None
    for candidate in records:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            status = (
                str(payload.get("status") or "unknown") if isinstance(payload, dict) else "unknown"
            )
            recovered_at = (
                str(payload.get("recovered_at"))
                if isinstance(payload, dict) and payload.get("recovered_at")
                else None
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            status = "unreadable"
            recovered_at = None
        if status != "completed":
            pending += 1
        latest_status = status
        latest_at = recovered_at
    return len(records), pending, latest_status, latest_at


def inspect_sync_checkpoint(
    path: Path,
    repo: str,
    policy: CheckpointPolicy | None = None,
) -> CheckpointInspection:
    checkpoint = Path(path)
    normalized = canonical_repo(repo)
    limits = policy or CheckpointPolicy()
    paths = sync_checkpoint_paths(checkpoint)
    state_name, state, version, error = _inspect_file(checkpoint, normalized)
    backup_name, _, _, backup_error = _inspect_file(paths.last_good, normalized)
    recovery_records, pending_recoveries, latest_recovery_status, latest_recovery_at = (
        _recovery_metadata(paths.recovery)
    )
    kind_counts: dict[str, int] = {}
    families: set[str] = set()
    if state is not None:
        for resource in state.resources.values():
            kind_counts[resource.kind] = kind_counts.get(resource.kind, 0) + 1
            families.add(resource_family(resource))
    return CheckpointInspection(
        repo=normalized,
        path=str(checkpoint),
        state=state_name,
        on_disk_version=version,
        bytes=checkpoint.stat().st_size if checkpoint.exists() else 0,
        resources=len(state.resources) if state is not None else 0,
        families=len(families),
        resource_kinds=dict(sorted(kind_counts.items())),
        conditional_responses=len(state.request_cache) if state is not None else 0,
        last_observed_at=state.last_observed_at if state is not None else None,
        last_compacted_at=state.last_compacted_at if state is not None else None,
        compacted_resources_total=(state.compacted_resources_total if state is not None else 0),
        compacted_families_total=(state.compacted_families_total if state is not None else 0),
        max_resources=limits.max_resources,
        max_bytes=limits.max_bytes,
        last_good_state=backup_name,
        last_good_bytes=paths.last_good.stat().st_size if paths.last_good.exists() else 0,
        last_good_error=backup_error,
        quarantine_files=(
            sum(candidate.is_file() for candidate in paths.quarantine.iterdir())
            if paths.quarantine.exists()
            else 0
        ),
        recovery_records=recovery_records,
        pending_recoveries=pending_recoveries,
        latest_recovery_status=latest_recovery_status,
        latest_recovery_at=latest_recovery_at,
        error=error,
    )


class CheckpointRecoveryResult(BaseModel):
    repo: str
    action: RecoveryAction
    outcome: Literal["dry_run", "completed"]
    primary_path: str
    source_path: str | None = None
    quarantine_path: str | None = None
    audit_path: str | None = None
    warning: str


def _audit_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def recover_sync_checkpoint(
    path: Path,
    repo: str,
    *,
    action: RecoveryAction,
    dry_run: bool,
    confirm_repo: str | None = None,
    recovered_at: str | None = None,
) -> CheckpointRecoveryResult:
    """Restore a verified backup or install an explicit empty rebaseline."""
    checkpoint = Path(path)
    normalized = canonical_repo(repo)
    paths = sync_checkpoint_paths(checkpoint)
    moment = to_iso(recovered_at) if recovered_at else to_iso(now_utc())
    primary_bytes = checkpoint.read_bytes() if checkpoint.exists() else b""
    primary_digest = hashlib.sha256(primary_bytes).hexdigest()

    if action == "restore_last_good":
        backup_state, _, _, backup_error = _inspect_file(paths.last_good, normalized)
        if backup_state != "healthy":
            raise SyncCheckpointError(
                f"verified last-good checkpoint is unavailable: {backup_error or backup_state}"
            )
        target_bytes = paths.last_good.read_bytes()
        source_path: str | None = str(paths.last_good)
        warning = "Restores the previous verified generation; the next poll replays any newer diff."
    else:
        target = RepoSyncState(repo=normalized)
        target_bytes = _checkpoint_bytes(target)
        source_path = None
        warning = (
            "Rebaseline forgets checkpoint fingerprints. Unchanged deliveries remain deduplicated; "
            "comment deletion is inferred only after a complete comment manifest is observed."
        )

    safe_moment = moment.replace(":", "").replace("-", "")
    quarantine_path = (
        paths.quarantine / f"{safe_moment}-{action}-{primary_digest[:12]}.json"
        if primary_bytes
        else None
    )
    operation_id = _sha256(
        {
            "repo": normalized,
            "action": action,
            "recovered_at": moment,
            "primary_sha256": primary_digest,
            "target_sha256": hashlib.sha256(target_bytes).hexdigest(),
        }
    )
    audit_path = paths.recovery / f"{safe_moment}-{operation_id[:16]}.json"
    result = CheckpointRecoveryResult(
        repo=normalized,
        action=action,
        outcome="dry_run" if dry_run else "completed",
        primary_path=str(checkpoint),
        source_path=source_path,
        quarantine_path=str(quarantine_path) if quarantine_path is not None else None,
        audit_path=str(audit_path),
        warning=warning,
    )
    if dry_run:
        return result
    if confirm_repo is None or canonical_repo(confirm_repo) != normalized:
        raise RecoveryConfirmationError(f"mutating recovery requires --confirm-repo {normalized}")

    audit = {
        **result.model_dump(mode="json"),
        "status": "planned",
        "recovered_at": moment,
        "primary_sha256": primary_digest,
        "target_sha256": hashlib.sha256(target_bytes).hexdigest(),
    }
    _atomic_write_bytes(audit_path, _audit_bytes(audit))
    if quarantine_path is not None:
        _atomic_write_bytes(quarantine_path, primary_bytes)
    _atomic_write_bytes(checkpoint, target_bytes)
    try:
        _decode_sync_state(checkpoint.read_bytes(), normalized)
    except Exception as exc:
        raise SyncCheckpointError("recovered checkpoint failed post-write validation") from exc
    audit["status"] = "completed"
    _atomic_write_bytes(audit_path, _audit_bytes(audit))
    return result
