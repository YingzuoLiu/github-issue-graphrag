from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from scripts.render_preapply_plan import APPROVAL_KEYS, PlanError, render_plan

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 lane
    tomllib = None

EXAMPLE = Path("deploy/aws-lightsail/preapply.example.json")


def _source():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_plan_is_deterministic_value_free_and_not_approval_ready():
    source = _source()
    first = render_plan(source)
    second = render_plan(source)
    assert first == second
    plan, dynamic, env = first
    assert plan["approval_ready"] is False
    assert plan["cloud_mutations_executed"] is False
    assert plan["approval_blockers"][0] == "example input cannot authorize apply"
    assert set(plan["approval_blockers"][1:]) == {
        f"approval missing: {key}" for key in APPROVAL_KEYS
    }
    assert plan["application"]["public_ask"] is False
    assert plan["application"]["openrouter_deployed"] is False
    assert plan["application"]["app_image"] == "github-issue-graphrag:example-only"
    assert plan["viewer_credential_boundary"].startswith("zero secret")
    assert plan["resources"]["budget_name"] == "issue-graphrag-prod-monthly"
    assert [row["consumers"] for row in plan["secrets"]] == [
        ["worker", "sync"],
        ["receiver"],
    ]
    assert "test-ambient-token" not in json.dumps(plan)
    assert "github_token=" not in env
    assert "github_webhook_secret=" not in env
    assert "radar.example.com" in dynamic


def test_edge_render_contains_tls_redirect_and_all_three_limits_per_lane():
    _, dynamic, _ = render_plan(_source())
    assert 'certResolver = "letsencrypt"' in dynamic
    assert '[http.middlewares.https-redirect.redirectScheme]' in dynamic
    assert dynamic.count(".rateLimit]") == 2
    assert dynamic.count(".inFlightReq]") == 2
    assert dynamic.count(".buffering]") == 2
    assert "maxRequestBodyBytes = 26214400" in dynamic
    assert "maxRequestBodyBytes = 1048576" in dynamic
    assert 'minVersion = "VersionTLS12"' in dynamic


@pytest.mark.skipif(tomllib is None, reason="tomllib is in the Python 3.11+ standard library")
def test_edge_render_is_valid_toml():
    _, dynamic, _ = render_plan(_source())
    parsed = tomllib.loads(dynamic)
    assert set(parsed) == {"http", "tls"}


def test_real_fully_approved_input_can_become_approval_ready():
    source = _source()
    source["example_only"] = False
    source["provider"]["account_id"] = "999999999999"
    source["network"]["hostname"] = "radar.user-domain.dev"
    source["application"]["github_repo"] = "owner/real-repository"
    source["application"]["app_image"] = f"github-issue-graphrag:{'a' * 40}"
    source["approvals"] = {key: True for key in APPROVAL_KEYS}
    plan, _, _ = render_plan(source)
    assert plan["approval_ready"] is True
    assert plan["approval_blockers"] == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["application"].update(public_ask=True), "Ask"),
        (lambda data: data["application"].update(openrouter_deployed=True), "OpenRouter"),
        (lambda data: data["application"].update(app_image="image:latest"), "non-latest"),
        (lambda data: data["network"].update(public_tcp_ports=[22, 80, 443]), "only TCP"),
        (lambda data: data["network"].update(admin_cidrs=["0.0.0.0/0"]), "too broad"),
        (lambda data: data["edge"].update(image="traefik:latest"), "pin"),
        (lambda data: data["backup"].update(automatic_disk_snapshot=False), "snapshots"),
        (lambda data: data["cost"].update(provider_hard_ceiling=True), "hard ceiling"),
        (lambda data: data["cost"].update(budget_alert_usd=10), "below expected"),
        (lambda data: data["cost"].update(budget_alert_email="not-an-email"), "email"),
        (lambda data: data["cost"].update(instance_monthly_usd=1), "official-price"),
        (lambda data: data["edge"]["viewer"].update(max_in_flight=1000), "limit contract"),
        (lambda data: data["resources"].update(disk_size_gib=8), "must be 20"),
    ],
)
def test_preapply_plan_rejects_unsafe_mutations(mutation, message):  # noqa: ANN001
    source = copy.deepcopy(_source())
    mutation(source)
    with pytest.raises(PlanError, match=message):
        render_plan(source)


def test_real_plan_rejects_reserved_hostname_and_non_sha_image():
    source = _source()
    source["example_only"] = False
    with pytest.raises(PlanError, match="40-character"):
        render_plan(source)
    source["application"]["app_image"] = f"github-issue-graphrag:{'b' * 40}"
    with pytest.raises(PlanError, match="real public hostname"):
        render_plan(source)


def test_apply_manifest_names_every_planned_resource_and_restricts_ssh():
    plan, _, _ = render_plan(_source())
    commands = plan["apply_commands_after_review_and_user_approval"]
    operations = [row[2] for row in commands]
    assert operations == [
        "create-instances",
        "allocate-static-ip",
        "attach-static-ip",
        "create-disk",
        "attach-disk",
        "put-instance-public-ports",
        "create-budget",
    ]
    port_command = commands[5]
    port_rows = json.loads(port_command[port_command.index("--port-infos") + 1])
    assert port_rows[0] == {
        "fromPort": 22,
        "toPort": 22,
        "protocol": "tcp",
        "cidrs": ["192.0.2.10/32"],
    }
    assert {row["fromPort"] for row in port_rows[1:]} == {80, 443}


def test_unknown_input_field_is_rejected_instead_of_hiding_a_secret_value():
    source = _source()
    source["github_token_value"] = "must-never-be-accepted"
    with pytest.raises(PlanError, match="fields mismatch"):
        render_plan(source)
