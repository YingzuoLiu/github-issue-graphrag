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
    if set(services["proxy"].get("profiles") or []) != {"local"}:
        raise ValueError("provider-neutral proxy must be restricted to the local profile")
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


def validate_public_compose(payload: dict[str, Any]) -> None:
    """Validate the merged provider-neutral and public edge configuration."""

    services = payload.get("services") or {}
    public_proxy = services.get("public-proxy")
    if not isinstance(public_proxy, dict):
        raise ValueError("missing public-proxy service")

    local_payload = {**payload, "services": {k: v for k, v in services.items() if k != "public-proxy"}}
    validate_compose(local_payload)

    if public_proxy.get("image") != "traefik:v3.6.14":
        raise ValueError("public-proxy image must pin traefik:v3.6.14")
    if set(public_proxy.get("profiles") or []) != {"public"}:
        raise ValueError("public-proxy must be restricted to the public profile")
    if public_proxy.get("read_only") is not True:
        raise ValueError("public-proxy root filesystem must be read-only")
    if _secret_names(public_proxy):
        raise ValueError("public-proxy must receive zero Compose secrets")
    published = sorted(int(row["published"]) for row in public_proxy.get("ports") or [])
    if published != [80, 443]:
        raise ValueError(f"public-proxy must publish only 80/443, observed {published}")
    mounts = public_proxy.get("volumes") or []
    if any("docker.sock" in str(row.get("source", "")) for row in mounts if isinstance(row, dict)):
        raise ValueError("public-proxy must not receive the Docker socket")
    dynamic = _mount(public_proxy, "/etc/traefik/dynamic.toml")
    acme = _mount(public_proxy, "/var/lib/traefik")
    if dynamic is None or not dynamic.get("read_only"):
        raise ValueError("public-proxy dynamic configuration must be read-only")
    if acme is None or acme.get("read_only"):
        raise ValueError("public-proxy ACME storage must be writable")

    command = {str(row) for row in public_proxy.get("command") or []}
    required = {
        "--providers.file.filename=/etc/traefik/dynamic.toml",
        "--providers.file.watch=false",
        "--accesslog.fields.headers.defaultmode=drop",
        "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web",
    }
    if not required <= command:
        raise ValueError("public-proxy static security configuration is incomplete")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rendered_json", type=Path)
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.rendered_json.read_text(encoding="utf-8"))
    if args.public:
        validate_public_compose(payload)
    else:
        validate_compose(payload)
    print("compose contract satisfied")


if __name__ == "__main__":
    main()
