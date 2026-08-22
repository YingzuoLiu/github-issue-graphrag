"""Receive GitHub webhooks and durably enqueue them for the live worker."""

from __future__ import annotations

import argparse
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.inbox import DeliveryInbox
from issue_graphrag.live.server import DEFAULT_MAX_BODY_BYTES, WebhookReceiver, create_http_server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="allowed owner/name; defaults to GITHUB_WEBHOOK_REPO")
    parser.add_argument("--secret", help="webhook secret; defaults to GITHUB_WEBHOOK_SECRET")
    parser.add_argument("--inbox", type=Path, default=None, help="SQLite inbox path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--max-body-mb",
        type=int,
        default=DEFAULT_MAX_BODY_BYTES // (1024 * 1024),
    )
    args = parser.parse_args()

    settings = load_settings()
    repo = args.repo or settings.github_webhook_repo
    secret = args.secret or settings.github_webhook_secret
    if not repo:
        parser.error("--repo or GITHUB_WEBHOOK_REPO is required")
    if not secret:
        parser.error("--secret or GITHUB_WEBHOOK_SECRET is required")

    inbox_path = args.inbox or settings.processed_data_dir / "webhook_inbox.sqlite"
    receiver = WebhookReceiver(
        secret=secret,
        repo=repo,
        inbox=DeliveryInbox(inbox_path),
        max_body_bytes=args.max_body_mb * 1024 * 1024,
    )
    server = create_http_server(receiver, host=args.host, port=args.port)
    print(f"Listening on http://{args.host}:{server.server_port}/webhooks/github")
    print(f"Accepting signed deliveries for {repo}; inbox: {inbox_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
