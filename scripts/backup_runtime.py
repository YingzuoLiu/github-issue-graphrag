"""Plan, create or restore one repository lane snapshot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.backup import BackupError, create_backup, restore_backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("repo", help="canonical owner/name repository lane")
    parser.add_argument("path", type=Path, help="backup output/input path")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--confirm-services-stopped", action="store_true")
    parser.add_argument("--confirm-repo")
    args = parser.parse_args()
    settings = load_settings()
    try:
        if args.action == "backup":
            result = create_backup(
                repo_data_dir=settings.repo_data_dir,
                analytics_path=settings.radar_analytics_path,
                repo=args.repo,
                output=args.path,
                dry_run=args.dry_run,
                services_stopped=args.confirm_services_stopped,
            )
        else:
            result = restore_backup(
                repo_data_dir=settings.repo_data_dir,
                analytics_path=settings.radar_analytics_path,
                repo=args.repo,
                backup_path=args.path,
                dry_run=args.dry_run,
                services_stopped=args.confirm_services_stopped,
                confirm_repo=args.confirm_repo,
            )
    except BackupError as exc:
        raise SystemExit(f"runtime backup operation refused: {exc}") from None
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
