"""Exit non-zero when one container's deterministic dependencies are not ready."""

from __future__ import annotations

import argparse
import sys

from issue_graphrag.config import load_settings
from issue_graphrag.live.backup import pending_restore_count
from issue_graphrag.live.operations import (
    probe_readable_path,
    probe_sqlite_readable,
    probe_writable_directory,
    validate_public_viewer,
)
from issue_graphrag.live.repositories import RepoRegistry
from issue_graphrag.live.sync_checkpoint import inspect_sync_checkpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=("viewer", "worker", "sync"))
    parser.add_argument("--repo")
    args = parser.parse_args()
    settings = load_settings()

    if args.component == "viewer":
        validate_public_viewer(settings)
        probe_readable_path(settings.repo_data_dir)
        for repo in settings.github_repos:
            if pending_restore_count(settings.repo_data_dir, repo):
                raise SystemExit(f"repository restore is incomplete: {repo}")
        try:
            probe_writable_directory(settings.radar_analytics_path.parent)
        except OSError as exc:
            print(f"viewer analytics degraded: {exc}", file=sys.stderr)
        print("viewer ready")
        return

    repo = args.repo or settings.github_webhook_repo
    if not repo:
        parser.error("--repo or GITHUB_WEBHOOK_REPO is required")
    paths = RepoRegistry(settings.repo_data_dir, settings.github_repos).paths(repo)
    probe_writable_directory(paths.root)
    probe_sqlite_readable(paths.inbox)
    if pending_restore_count(settings.repo_data_dir, paths.repo):
        raise SystemExit("repository restore is incomplete")

    if args.component == "sync":
        checkpoint = inspect_sync_checkpoint(paths.sync_state, paths.repo)
        recovery_required = (
            checkpoint.state == "corrupt"
            or checkpoint.pending_recoveries
            or (checkpoint.state == "missing" and checkpoint.last_good_state != "missing")
        )
        if recovery_required:
            raise SystemExit("sync checkpoint requires operator recovery")
    print(f"{args.component} ready")


if __name__ == "__main__":
    main()
