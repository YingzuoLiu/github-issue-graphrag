"""Repository-qualified storage layout, registry and freshness metadata."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from issue_graphrag.ingest.github_loader import parse_repo

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")


def canonical_repo(repo: str) -> str:
    """Validate and normalize one GitHub owner/repository identifier."""
    owner, name = parse_repo(repo)
    if (
        owner in {".", ".."}
        or name in {".", ".."}
        or not _SAFE_COMPONENT.fullmatch(owner)
        or not _SAFE_COMPONENT.fullmatch(name)
    ):
        raise ValueError("repo components may contain only letters, numbers, '.', '_' and '-'")
    return f"{owner.casefold()}/{name.casefold()}"


def repo_directory_name(repo: str) -> str:
    owner, name = canonical_repo(repo).split("/", 1)
    return f"{owner}__{name}"


@dataclass(frozen=True)
class RepoPaths:
    repo: str
    root: Path
    state: Path
    event_log: Path
    inbox: Path
    bootstrap_seed: Path
    extraction_cache: Path
    freshness: Path


def repo_paths(root: Path, repo: str) -> RepoPaths:
    normalized = canonical_repo(repo)
    directory = Path(root) / repo_directory_name(normalized)
    return RepoPaths(
        repo=normalized,
        root=directory,
        state=directory / "live_state.json",
        event_log=directory / "event_log.jsonl",
        inbox=directory / "inbox.db",
        bootstrap_seed=directory / "bootstrap_seed.json",
        extraction_cache=directory / "extraction_cache.sqlite",
        freshness=directory / "freshness.json",
    )


class RepoFreshness(BaseModel):
    repo: str
    last_source_sync_at: str | None = None
    last_state_commit_at: str | None = None
    semantic_status: Literal["not_started", "pending", "current", "degraded"] = "not_started"
    semantic_updated_at: str | None = None
    last_error: str | None = None


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def read_freshness(path: Path, repo: str) -> RepoFreshness:
    normalized = canonical_repo(repo)
    if not path.exists():
        return RepoFreshness(repo=normalized)
    with path.open("r", encoding="utf-8") as handle:
        freshness = RepoFreshness.model_validate(json.load(handle))
    if canonical_repo(freshness.repo) != normalized:
        raise ValueError(f"freshness belongs to {freshness.repo!r}, not {normalized!r}")
    freshness.repo = normalized
    return freshness


def write_freshness(path: Path, freshness: RepoFreshness) -> None:
    freshness.repo = canonical_repo(freshness.repo)
    _write_json(path, freshness.model_dump(mode="json"))


class RepoRegistry:
    """Operator-configured repositories backed by one small registry file."""

    def __init__(self, root: Path, configured: tuple[str, ...] = ()):
        self.root = Path(root)
        self.path = self.root / "repositories.json"
        self.configured = tuple(canonical_repo(repo) for repo in configured)

    def repositories(self) -> list[str]:
        stored: list[str] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            rows = payload.get("repositories") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise ValueError("repository registry must contain a repositories list")
            stored = [canonical_repo(str(repo)) for repo in rows]
        return sorted(set(self.configured) | set(stored))

    def register(self, repo: str) -> RepoPaths:
        normalized = canonical_repo(repo)
        repositories = sorted(set(self.repositories()) | {normalized})
        _write_json(self.path, {"version": 1, "repositories": repositories})
        paths = repo_paths(self.root, normalized)
        paths.root.mkdir(parents=True, exist_ok=True)
        return paths

    def paths(self, repo: str) -> RepoPaths:
        normalized = canonical_repo(repo)
        if normalized not in self.repositories():
            raise ValueError(f"repository is not configured: {normalized}")
        return repo_paths(self.root, normalized)
