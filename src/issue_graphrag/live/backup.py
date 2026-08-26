"""Validated, operator-confirmed snapshots for one repository deployment lane."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from issue_graphrag.live.repositories import canonical_repo, repo_directory_name, repo_paths
from issue_graphrag.live.timeutil import now_utc, to_iso

BACKUP_VERSION = 1
COPY_CHUNK_BYTES = 1024 * 1024


class BackupError(RuntimeError):
    """A backup or restore cannot be proved safe."""


@dataclass(frozen=True)
class BackupResult:
    repo: str
    outcome: str
    backup_path: str
    files: int
    bytes: int
    audit_path: str | None = None
    quarantine_path: str | None = None
    analytics_quarantine_path: str | None = None


def _copy_chunks(source: BinaryIO, target: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(COPY_CHUNK_BYTES):
        target.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    return size, digest.hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
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
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_copy(source: Path, target: Path) -> tuple[int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with source.open("rb") as source_handle, tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as target_handle:
            temporary = Path(target_handle.name)
            size, digest = _copy_chunks(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.replace(temporary, target)
        return size, digest
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _file_identity(path: Path) -> tuple[int, str]:
    with path.open("rb") as handle:
        sink = hashlib.sha256()
        size = 0
        while chunk := handle.read(COPY_CHUNK_BYTES):
            sink.update(chunk)
            size += len(chunk)
    return size, sink.hexdigest()


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def _source_files(repo_data_dir: Path, analytics_path: Path, repo: str) -> dict[str, Path]:
    lane = repo_paths(repo_data_dir, repo).root
    if not lane.is_dir():
        raise BackupError(f"repository lane does not exist: {lane}")
    files: dict[str, Path] = {}
    for source in sorted(lane.rglob("*")):
        if source.is_symlink():
            raise BackupError(f"snapshot refuses symlink: {source}")
        if source.is_file() and not source.name.endswith(".tmp"):
            files[f"repo/{source.relative_to(lane).as_posix()}"] = source
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{analytics_path}{suffix}")
        if source.is_symlink():
            raise BackupError(f"snapshot refuses symlink: {source}")
        if source.is_file():
            files[f"analytics/radar.sqlite{suffix}"] = source
    return files


def _read_manifest(
    backup_path: Path,
    repo: str,
) -> tuple[dict[str, Any], dict[str, tuple[Path, int, str]]]:
    manifest_path = backup_path / "manifest.json"
    if manifest_path.is_symlink():
        raise BackupError("backup manifest must not be a symlink")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid backup manifest: {exc}") from exc
    normalized = canonical_repo(repo)
    if manifest.get("version") != BACKUP_VERSION or manifest.get("repo") != normalized:
        raise BackupError("backup version or repository does not match")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise BackupError("backup manifest files must be a list")
    payloads: dict[str, tuple[Path, int, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise BackupError("backup manifest contains an invalid file row")
        relative = row["path"]
        parts = Path(relative).parts
        analytics_names = {
            "analytics/radar.sqlite",
            "analytics/radar.sqlite-wal",
            "analytics/radar.sqlite-shm",
        }
        valid_repo = len(parts) > 1 and parts[0] == "repo"
        if (
            not parts
            or Path(relative).is_absolute()
            or ".." in parts
            or (not valid_repo and relative not in analytics_names)
        ):
            raise BackupError(f"unsafe backup path: {relative}")
        if relative in payloads:
            raise BackupError(f"duplicate backup payload: {relative}")
        source = backup_path / "payload" / relative
        if source.is_symlink() or not source.is_file():
            raise BackupError(f"backup payload must be a regular file: {relative}")
        expected_bytes = row.get("bytes")
        expected_sha256 = row.get("sha256")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
        ):
            raise BackupError(f"backup payload metadata is invalid: {relative}")
        actual_bytes, actual_sha256 = _file_identity(source)
        if actual_bytes != expected_bytes or actual_sha256 != expected_sha256:
            raise BackupError(f"backup payload failed validation: {relative}")
        payloads[relative] = (source, expected_bytes, expected_sha256)
    payload_root = backup_path / "payload"
    observed: set[str] = set()
    if payload_root.exists():
        for source in payload_root.rglob("*"):
            if source.is_symlink():
                raise BackupError(f"backup payload must not be a symlink: {source}")
            if source.is_file():
                observed.add(source.relative_to(payload_root).as_posix())
    if observed != set(payloads):
        raise BackupError("backup payload set does not match the manifest")
    return manifest, payloads


def create_backup(
    *,
    repo_data_dir: Path,
    analytics_path: Path,
    repo: str,
    output: Path,
    dry_run: bool,
    services_stopped: bool,
    created_at: str | None = None,
) -> BackupResult:
    normalized = canonical_repo(repo)
    files = _source_files(repo_data_dir, analytics_path, normalized)
    total_bytes = sum(path.stat().st_size for path in files.values())
    result = BackupResult(
        repo=normalized,
        outcome="dry_run" if dry_run else "completed",
        backup_path=str(output),
        files=len(files),
        bytes=total_bytes,
    )
    if dry_run:
        return result
    if not services_stopped:
        raise BackupError("backup requires --confirm-services-stopped")
    destination = Path(output)
    if destination.exists():
        raise BackupError(f"backup destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        rows = []
        for relative, source in sorted(files.items()):
            target = stage / "payload" / relative
            size, digest = _atomic_copy(source, target)
            rows.append({"path": relative, "bytes": size, "sha256": digest})
        manifest = {
            "version": BACKUP_VERSION,
            "repo": normalized,
            "created_at": to_iso(created_at) if created_at else to_iso(now_utc()),
            "files": rows,
        }
        _atomic_write(stage / "manifest.json", _json_bytes(manifest))
        os.replace(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return result


def _restore_audit_path(repo_data_dir: Path, repo: str, restored_at: str) -> Path:
    safe_time = restored_at.replace(":", "").replace("-", "")
    return (
        Path(repo_data_dir)
        / ".operations"
        / "restores"
        / repo_directory_name(repo)
        / f"{safe_time}.json"
    )


def pending_restore_count(repo_data_dir: Path, repo: str) -> int:
    directory = _restore_audit_path(repo_data_dir, repo, "2000-01-01T00:00:00Z").parent
    if not directory.exists():
        return 0
    pending = 0
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pending += 1
            continue
        if payload.get("status") != "completed":
            pending += 1
    return pending


def restore_backup(
    *,
    repo_data_dir: Path,
    analytics_path: Path,
    repo: str,
    backup_path: Path,
    dry_run: bool,
    services_stopped: bool,
    confirm_repo: str | None,
    restored_at: str | None = None,
) -> BackupResult:
    normalized = canonical_repo(repo)
    _, payloads = _read_manifest(Path(backup_path), normalized)
    total_bytes = sum(row[1] for row in payloads.values())
    moment = to_iso(restored_at) if restored_at else to_iso(now_utc())
    audit_path = _restore_audit_path(repo_data_dir, normalized, moment)
    quarantine = audit_path.parent.parent.parent / "restore-quarantine" / audit_path.stem
    analytics_quarantine = (
        Path(analytics_path).parent
        / ".restore-quarantine"
        / repo_directory_name(normalized)
        / audit_path.stem
    )
    result = BackupResult(
        repo=normalized,
        outcome="dry_run" if dry_run else "completed",
        backup_path=str(backup_path),
        files=len(payloads),
        bytes=total_bytes,
        audit_path=str(audit_path),
        quarantine_path=str(quarantine),
        analytics_quarantine_path=str(analytics_quarantine),
    )
    if dry_run:
        return result
    if not services_stopped:
        raise BackupError("restore requires --confirm-services-stopped")
    if confirm_repo is None or canonical_repo(confirm_repo) != normalized:
        raise BackupError(f"restore requires --confirm-repo {normalized}")

    lane = repo_paths(repo_data_dir, normalized).root
    lane.parent.mkdir(parents=True, exist_ok=True)
    Path(analytics_path).parent.mkdir(parents=True, exist_ok=True)
    repo_stage = Path(tempfile.mkdtemp(prefix=f".{lane.name}.restore.", dir=lane.parent))
    analytics_stage = Path(
        tempfile.mkdtemp(prefix=".analytics.restore.", dir=Path(analytics_path).parent)
    )
    try:
        for relative, (source, expected_bytes, expected_digest) in payloads.items():
            if relative.startswith("repo/"):
                target = repo_stage / relative.removeprefix("repo/")
            else:
                target = analytics_stage / Path(relative).name
            copied_bytes, copied_digest = _atomic_copy(source, target)
            if copied_bytes != expected_bytes or copied_digest != expected_digest:
                raise BackupError(f"backup payload changed during restore: {relative}")

        quarantine.mkdir(parents=True, exist_ok=False)
        analytics_quarantine.mkdir(parents=True, exist_ok=False)
        operation = {
            "version": 1,
            "repo": normalized,
            "status": "planned",
            "restored_at": moment,
            "backup_path": str(backup_path),
            "quarantine_path": str(quarantine),
            "analytics_quarantine_path": str(analytics_quarantine),
        }
        _atomic_write(audit_path, _json_bytes(operation))

        if lane.exists():
            os.replace(lane, quarantine / "repo")
        os.replace(repo_stage, lane)
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{analytics_path}{suffix}")
            if target.exists():
                os.replace(target, analytics_quarantine / target.name)
            staged = analytics_stage / f"radar.sqlite{suffix}"
            if staged.exists():
                os.replace(staged, target)
        operation["status"] = "completed"
        _atomic_write(audit_path, _json_bytes(operation))
    finally:
        if repo_stage.exists():
            shutil.rmtree(repo_stage)
        if analytics_stage.exists():
            shutil.rmtree(analytics_stage)
    return result
