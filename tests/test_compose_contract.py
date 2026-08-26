from __future__ import annotations

import copy

import pytest

from scripts.check_compose_contract import validate_compose


def _service(*, secrets=(), ports=(), volumes=(), environment=None):  # noqa: ANN001
    return {
        "read_only": True,
        "secrets": [{"source": secret} for secret in secrets],
        "ports": list(ports),
        "volumes": list(volumes),
        "environment": environment or {},
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
            "proxy": _service(ports=({"published": 8080},)),
            "backup": _service(),
        }
    }


def test_rendered_compose_contract_accepts_minimum_privilege_topology():
    validate_compose(_rendered())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["services"]["viewer"]["secrets"].append({"source": "github_token"}), "zero"),
        (lambda data: data["services"]["viewer"]["volumes"][0].update(read_only=False), "read-only"),
        (lambda data: data["services"]["receiver"].update(ports=[{"published": 8000}]), "only proxy"),
        (lambda data: data["services"]["worker"].update(secrets=[]), "secret boundary"),
        (lambda data: data["services"]["backup"].update(secrets=[{"source": "github_token"}]), "secret boundary"),
        (lambda data: data["services"]["proxy"].update(read_only=False), "read-only"),
    ],
)
def test_rendered_compose_contract_rejects_boundary_regressions(mutation, message):  # noqa: ANN001
    payload = copy.deepcopy(_rendered())
    mutation(payload)
    with pytest.raises(ValueError, match=message):
        validate_compose(payload)
