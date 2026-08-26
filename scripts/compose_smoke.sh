#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
smoke_root="$(mktemp -d)"
compose_project="graphrag-smoke-${RANDOM}"

cleanup() {
  if docker image inspect github-issue-graphrag:0.4.0-dev >/dev/null 2>&1; then
    docker compose \
      --project-directory "${project_root}" \
      --project-name "${compose_project}" \
      --env-file "${smoke_root}/smoke.env" \
      --profile ops run --rm --no-deps --user root --entrypoint chmod backup \
      -R a+rwX /var/lib/issue-graphrag/repos /var/lib/issue-graphrag/analytics \
      /var/lib/issue-graphrag/backups >/dev/null 2>&1 || true
  fi
  docker compose \
    --project-directory "${project_root}" \
    --project-name "${compose_project}" \
    --env-file "${smoke_root}/smoke.env" \
    down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf "${smoke_root}"
}
trap cleanup EXIT

mkdir -p \
  "${smoke_root}/repos" \
  "${smoke_root}/analytics" \
  "${smoke_root}/backups" \
  "${smoke_root}/secrets"
chmod 0777 "${smoke_root}/repos" "${smoke_root}/analytics" "${smoke_root}/backups"
printf '%s\n' 'smoke-read-only-token' >"${smoke_root}/secrets/github_token"
printf '%s\n' 'smoke-webhook-secret' >"${smoke_root}/secrets/github_webhook_secret"
# Docker Compose implements local file-backed secrets as bind mounts. The parent
# temporary directory is 0700; make only the mounted files readable by UID 10001.
chmod 0444 "${smoke_root}/secrets/github_token" "${smoke_root}/secrets/github_webhook_secret"

cat >"${smoke_root}/smoke.env" <<EOF
GITHUB_REPO=trustgraph-ai/trustgraph
PUBLIC_HTTP_PORT=18080
SITE_ADDRESS=:8080
REPO_DATA_HOST=${smoke_root}/repos
RADAR_ANALYTICS_HOST=${smoke_root}/analytics
BACKUP_DATA_HOST=${smoke_root}/backups
GITHUB_TOKEN_FILE_HOST=${smoke_root}/secrets/github_token
GITHUB_WEBHOOK_SECRET_FILE_HOST=${smoke_root}/secrets/github_webhook_secret
EOF

compose() {
  docker compose \
    --project-directory "${project_root}" \
    --project-name "${compose_project}" \
    --env-file "${smoke_root}/smoke.env" \
    "$@"
}

wait_http() {
  local url="$1"
  local expected="$2"
  local status=""
  for _ in $(seq 1 60); do
    status="$(curl --silent --output /dev/null --write-out '%{http_code}' "${url}" || true)"
    if [[ "${status}" == "${expected}" ]]; then
      return 0
    fi
    sleep 1
  done
  echo "timed out waiting for ${url}: expected ${expected}, observed ${status}" >&2
  return 1
}

wait_service_healthy() {
  local service="$1"
  local container_id=""
  local status=""
  for _ in $(seq 1 60); do
    container_id="$(compose ps --all --quiet "${service}")"
    if [[ -n "${container_id}" ]]; then
      status="$(docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "${container_id}" 2>/dev/null || true)"
      if [[ "${status}" == "healthy" ]]; then
        return 0
      fi
      if [[ "${status}" == "exited" || "${status}" == "dead" ]]; then
        compose logs "${service}" >&2 || true
        echo "${service} stopped before becoming healthy: ${status}" >&2
        return 1
      fi
    fi
    sleep 1
  done
  compose ps "${service}" >&2 || true
  compose logs "${service}" >&2 || true
  echo "timed out waiting for ${service} to become healthy: ${status}" >&2
  return 1
}

compose --profile live --profile ops config --format json >"${smoke_root}/compose.json"
"${project_root}/.venv/bin/python" "${project_root}/scripts/check_compose_contract.py" \
  "${smoke_root}/compose.json" 2>/dev/null \
  || python "${project_root}/scripts/check_compose_contract.py" "${smoke_root}/compose.json"

compose build receiver
compose up --detach receiver worker viewer proxy
wait_http "http://127.0.0.1:18080/" 200

receiver_id="$(compose ps --quiet receiver)"
wait_http "http://127.0.0.1:18080/webhooks/github" 404
compose exec --no-TTY receiver python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez').read()"
compose exec --no-TTY receiver python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz').read()"

compose stop worker
python - "${project_root}/fixtures/live_demo/events/001-issue_comment-created-922.json" \
  "${smoke_root}/payload.json" "${smoke_root}/signature" <<'PY'
import hashlib
import hmac
import json
import sys
from pathlib import Path

fixture = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
body = json.dumps(fixture["payload"], separators=(",", ":")).encode()
Path(sys.argv[2]).write_bytes(body)
signature = hmac.new(b"smoke-webhook-secret", body, hashlib.sha256).hexdigest()
Path(sys.argv[3]).write_text(f"sha256={signature}", encoding="utf-8")
PY
webhook_response="$(curl --fail-with-body --silent --show-error \
  --request POST \
  --header 'Content-Type: application/json' \
  --header 'X-GitHub-Delivery: smoke-durable-1' \
  --header 'X-GitHub-Event: issue_comment' \
  --header "X-Hub-Signature-256: $(<"${smoke_root}/signature")" \
  --data-binary "@${smoke_root}/payload.json" \
  http://127.0.0.1:18080/webhooks/github)"
python - "${webhook_response}" <<'PY'
import json
import sys

response = json.loads(sys.argv[1])
if response.get("status") != "enqueued":
    raise SystemExit(f"webhook was not durably enqueued: {response}")
PY
compose restart receiver viewer proxy
wait_http "http://127.0.0.1:18080/" 200
compose run --rm --no-deps worker python scripts/process_webhooks.py \
  --repo trustgraph-ai/trustgraph --once
worker_status="$(compose run --rm --no-deps worker python scripts/process_webhooks.py \
  --repo trustgraph-ai/trustgraph --status)"
printf '%s\n' "${worker_status}"
grep -q 'succeeded: 1' <<<"${worker_status}"
compose start worker
wait_service_healthy worker
compose exec --no-TTY worker python scripts/operations_readiness.py \
  worker --repo trustgraph-ai/trustgraph

compose exec --user root --no-TTY receiver \
  chmod -R a-w /var/lib/issue-graphrag/repos/trustgraph-ai__trustgraph
compose exec --no-TTY receiver python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/livez').read()"
if compose exec --no-TTY receiver python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz').read()"; then
  echo "receiver readiness stayed healthy after durable storage became read-only" >&2
  exit 1
fi
compose exec --user root --no-TTY receiver \
  chmod -R ugo+w /var/lib/issue-graphrag/repos/trustgraph-ai__trustgraph
compose exec --no-TTY receiver python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz').read()"

state_path="${smoke_root}/repos/trustgraph-ai__trustgraph/live_state.json"
state_hash="$(sha256sum "${state_path}" | awk '{print $1}')"
compose stop proxy viewer worker receiver
compose --profile ops run --rm backup \
  backup trustgraph-ai/trustgraph /var/lib/issue-graphrag/backups/smoke \
  --confirm-services-stopped
printf '%s\n' '{"corrupt": true}' >"${state_path}"
compose --profile ops run --rm backup \
  restore trustgraph-ai/trustgraph /var/lib/issue-graphrag/backups/smoke \
  --confirm-services-stopped --confirm-repo trustgraph-ai/trustgraph
test "$(sha256sum "${state_path}" | awk '{print $1}')" = "${state_hash}"
compose start receiver worker viewer proxy
wait_http "http://127.0.0.1:18080/" 200
wait_service_healthy worker
compose exec --no-TTY worker python scripts/operations_readiness.py \
  worker --repo trustgraph-ai/trustgraph

test -n "${receiver_id}"
echo "compose smoke satisfied"
