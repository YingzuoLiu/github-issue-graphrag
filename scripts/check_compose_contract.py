"""Validate the rendered, provider-neutral Compose security boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SECRET_NAMES = {"github_token", "github_webhook_secret", "llm_api_key"}
READ_ONLY_SERVICES = {"viewer", "receiver", "worker", "sync", "proxy", "backup"}


def _secret_names(service: dict[str, Any]) -> set[str]:
    names = set()
    for row in service.get("secrets") or []:
        names.add(str(row.get("source")) if isinstance(row, dict) else str(row))
    return names


def _environment(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    result = {}
    for row in environment:
        key, _, value = str(row).partition("=")
        result[key] = value
    return result


def _mount(service: dict[str, Any], target: str) -> dict[str, Any] | None:
    for row in service.get("volumes") or []:
        if isinstance(row, dict) and row.get("target") == target:
            return row
    return None


def validate_compose(payload: dict[str, Any]) -> None:
    services = payload.get("services") or {}
    missing = {"viewer", "receiver", "worker", "sync", "proxy", "backup"} - set(services)
    if missing:
        raise ValueError(f"missing services: {sorted(missing)}")
    published = [name for name, service in services.items() if service.get("ports")]
    if published != ["proxy"]:
        raise ValueError(f"only proxy may publish ports, observed {published}")
    for name in READ_ONLY_SERVICES:
        if services[name].get("read_only") is not True:
            raise ValueError(f"{name} root filesystem must be read-only")

    viewer = services["viewer"]
    viewer_env = _environment(viewer)
    if viewer_env.get("PUBLIC_RADAR_ONLY") not in {"1", "true", "True"}:
        raise ValueError("viewer must enable PUBLIC_RADAR_ONLY")
    if _secret_names(viewer):
        raise ValueError("viewer must receive zero Compose secrets")
    credential_names = ("GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET", "LLM_API_KEY")
    if any(name in viewer_env or f"{name}_FILE" in viewer_env for name in credential_names):
        raise ValueError("viewer environment exposes a credential name")
    repo_mount = _mount(viewer, "/var/lib/issue-graphrag/repos")
    analytics_mount = _mount(viewer, "/var/lib/issue-graphrag/analytics")
    if repo_mount is None or not repo_mount.get("read_only"):
        raise ValueError("viewer repository data mount must be read-only")
    if analytics_mount is None or analytics_mount.get("read_only"):
        raise ValueError("viewer analytics mount must be independently writable")

    expected = {
        "receiver": {"github_webhook_secret"},
        "worker": {"github_token"},
        "sync": {"github_token"},
        "proxy": set(),
        "backup": set(),
    }
    for name, allowed in expected.items():
        actual = _secret_names(services[name])
        if actual != allowed or actual - SECRET_NAMES:
            raise ValueError(f"{name} secret boundary mismatch: {sorted(actual)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered_json", type=Path)
    args = parser.parse_args()
    validate_compose(json.loads(args.rendered_json.read_text(encoding="utf-8")))
    print("compose contract satisfied")


if __name__ == "__main__":
    main()
