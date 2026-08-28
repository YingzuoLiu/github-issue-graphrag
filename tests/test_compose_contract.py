from __future__ import annotations

import copy

import pytest

from scripts.check_compose_contract import validate_compose, validate_public_compose


def _service(*, secrets=(), ports=(), volumes=(), environment=None, profiles=()):  # noqa: ANN001
    return {
        "read_only": True,
        "secrets": [{"source": secret} for secret in secrets],
        "ports": list(ports),
        "volumes": list(volumes),
        "environment": environment or {},
        "profiles": list(profiles),
    }


def _rendered():
    repo_ro = {
        "type": "bind",
        "source": "/host/repos",
        "target": "/var/lib/issue-graphrag/repos",
        "read_only": True,
    }
    analytics_rw = {
        "type": "bind",
        "source": "/host/analytics",
        "target": "/var/lib/issue-graphrag/analytics",
        "read_only": False,
    }
    return {
        "services": {
            "viewer": _service(
                volumes=(repo_ro, analytics_rw),
                environment={"PUBLIC_RADAR_ONLY": "1"},
            ),
            "receiver": _service(secrets=("github_webhook_secret",)),
            "worker": _service(secrets=("github_token",)),
            "sync": _service(secrets=("github_token",)),
            "proxy": _service(ports=({"published": 8080},), profiles=("local",)),
            "backup": _service(),
        }
    }


def test_rendered_compose_contract_accepts_minimum_privilege_topology():
    validate_compose(_rendered())


def _public_proxy():
    return {
        "image": "traefik:v3.6.14",
        "profiles": ["public"],
        "read_only": True,
        "secrets": [],
        "ports": [{"published": 80}, {"published": 443}],
        "volumes": [
            {
                "type": "bind",
                "source": "/host/dynamic.toml",
                "target": "/etc/traefik/dynamic.toml",
                "read_only": True,
            },
            {
                "type": "bind",
                "source": "/host/acme",
                "target": "/var/lib/traefik",
                "read_only": False,
            },
        ],
        "command": [
            "--providers.file.filename=/etc/traefik/dynamic.toml",
            "--providers.file.watch=false",
            "--accesslog.fields.headers.defaultmode=drop",
            "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=web",
        ],
    }


def test_public_compose_contract_accepts_tls_edge_without_credentials_or_socket():
    payload = _rendered()
    payload["services"]["public-proxy"] = _public_proxy()
    validate_public_compose(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["services"]["viewer"]["secrets"].append({"source": "github_token"}), "zero"),
        (lambda data: data["services"]["viewer"]["volumes"][0].update(read_only=False), "read-only"),
        (lambda data: data["services"]["receiver"].update(ports=[{"published": 8000}]), "only proxy"),
        (lambda data: data["services"]["worker"].update(secrets=[]), "secret boundary"),
        (lambda data: data["services"]["backup"].update(secrets=[{"source": "github_token"}]), "secret boundary"),
        (lambda data: data["services"]["proxy"].update(read_only=False), "read-only"),
        (lambda data: data["services"]["proxy"].update(profiles=[]), "local profile"),
    ],
)
def test_rendered_compose_contract_rejects_boundary_regressions(mutation, message):  # noqa: ANN001
    payload = copy.deepcopy(_rendered())
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        validate_compose(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(image="traefik:latest"), "pin"),
        (lambda data: data.update(profiles=[]), "public profile"),
        (lambda data: data.update(secrets=[{"source": "github_token"}]), "zero"),
        (lambda data: data["ports"].append({"published": 8080}), "only 80/443"),
        (
            lambda data: data["volumes"].append(
                {"source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}
            ),
            "Docker socket",
        ),
        (lambda data: data.update(command=[]), "incomplete"),
    ],
)
def test_public_compose_contract_rejects_edge_regressions(mutation, message):  # noqa: ANN001
    payload = _rendered()
    public_proxy = _public_proxy()
    mutation(public_proxy)
    payload["services"]["public-proxy"] = public_proxy
    with pytest.raises(ValueError, match=message):
        validate_public_compose(payload)
