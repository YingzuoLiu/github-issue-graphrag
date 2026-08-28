# Deterministic single-host operations foundation

This document covers the provider-neutral M7 operations layer. It is safe to render, test and
review without creating cloud resources. It does **not** claim that the product is internet-ready:
provider selection, durable-volume sizing, TLS hostname and renewal, connection/rate limits,
production secrets, cost approval and teardown remain in the pre-apply package.

## Why these boundaries exist

The application already had a Streamlit Viewer, signed webhook receiver, durable inbox worker and
scheduled GitHub synchronizer. Running those commands in one shell would preserve functionality,
but it would not preserve the product's security and recovery properties: the Viewer could inherit
execution credentials, analytics could share a writable data mount, liveness could hide a failed
durable volume, and an interrupted restore could be served as healthy.

Compose makes those deterministic boundaries executable. No LLM or agent decides whether a
process may see a secret, whether a mount is writable, whether a restore is complete, or whether a
service is ready.

## Process and credential topology

| Service | Published network | Durable access | Secrets | Responsibility |
|---|---|---|---|---|
| `proxy` | host HTTP port; explicit `local` profile only | none | none | Caddy routing for local/CI smoke |
| `public-proxy` | host TCP 80/443; `public` profile | ACME state RW | none | reviewed TLS, body, concurrency and rate limits |
| `viewer` | internal `8501` only | repo data RO; analytics RW | none | public Contribution Radar only |
| `receiver` | internal `8000` only | repo data RW | webhook HMAC secret | verify and durably enqueue |
| `worker` | none | repo data RW | read-only GitHub token | single-writer state commit; facts only in this foundation |
| `sync` | none; `live` profile | repo data RW | read-only GitHub token | bounded read-only reconciliation |
| `backup` | none; one-shot `ops` profile | repo, analytics and backup RW | none | validated offline snapshot/restore |

The Viewer starts with `PUBLIC_RADAR_ONLY=1`. Startup fails if it sees a GitHub token, webhook
secret or LLM key, or if its analytics database is configured inside the read-only repository
tree. An analytics write failure is reported as degraded but does not take the Radar out of
service. Public Ask and production OpenRouter extraction remain disabled here.

GitHub bootstrap, worker hydration and scheduled synchronization all dispatch through the same
`CountingSession` boundary. It counts every request and refuses a non-read-only method before the
underlying HTTP session can send it.

## Persistent paths and secrets

Copy `.env.deploy.example` to an ignored `.env.deploy`, create the three host directories, and
write the two secret values to files outside Git:

```bash
cp .env.deploy.example .env.deploy
mkdir -p deploy-data/repos deploy-data/analytics backups secrets
printf '%s\n' 'replace-with-read-only-token' > secrets/github_token
printf '%s\n' 'replace-with-random-webhook-secret' > secrets/github_webhook_secret
sudo chown -R 10001:10001 deploy-data backups secrets
chmod 700 secrets
chmod 400 secrets/github_token secrets/github_webhook_secret
```

`GITHUB_TOKEN_FILE_HOST` and `GITHUB_WEBHOOK_SECRET_FILE_HOST` are path names, not secret values.
The application also supports `GITHUB_TOKEN_FILE`, `GITHUB_WEBHOOK_SECRET_FILE` and
`LLM_API_KEY_FILE`; setting both a direct value and its `_FILE` form fails closed.

Render and validate the exact security topology before startup:

```bash
docker compose --env-file .env.deploy --profile local --profile live --profile ops \
  config --format json > /tmp/issue-graphrag-compose.json
python scripts/check_compose_contract.py /tmp/issue-graphrag-compose.json
```

Only the local `proxy` may publish a port in this foundation. The provider overlay separately
requires `public-proxy` to publish exactly 80/443, pin its image, receive no credentials or Docker
socket, and mount only its generated routing file read-only plus ACME state read-write. A rendered
config that gives the Viewer a secret or a writable repo-data mount is rejected.

## Startup, health and shutdown

Start the local HTTP foundation:

```bash
docker compose --env-file .env.deploy up --build -d receiver worker viewer proxy
```

Add `--profile live` when the real GitHub token and repository are ready for scheduled sync. The
provider path uses `compose.public.yaml` and the generated pre-apply artifacts documented in
`docs/m7-preapply-package.md`; it never starts the local Caddy service.

Receiver endpoints deliberately have different semantics:

- `/livez` and compatibility `/healthz`: the HTTP process can answer; no storage assertion.
- `/readyz`: the inbox opens, its durable directory accepts an fsynced probe, and no restore audit
  is incomplete.

Worker and sync health checks validate the repo lane, SQLite inbox and restore state. Sync also
fails readiness for a corrupt checkpoint or pending checkpoint recovery. Viewer readiness checks
public zero-credential mode, readable repo data and incomplete restore records; analytics failure
is logged as degraded without blocking the product path. Stale GitHub data is a visible product
state, not a reason to restart an otherwise healthy service.

Receiver, worker and sync translate `SIGTERM`/`SIGINT` into cooperative stop events. Their loops do
not wait out a full poll interval after shutdown is requested. Compose grants a 30-second stop
window and uses an init process for signal forwarding and child reaping.

Inspect service and queue state without exposing a public management route:

```bash
docker compose --env-file .env.deploy ps
docker compose --env-file .env.deploy exec worker \
  python scripts/process_webhooks.py --repo owner/name --status
docker compose --env-file .env.deploy --profile live exec sync \
  python scripts/sync_repositories.py owner/name --status
```

## Validated backup and restore

The snapshot contains exactly one repo lane plus the analytics SQLite database and sidecars. Every
payload has a byte count and SHA-256 manifest entry. Symlinks are refused. Restore validates every
payload before changing a target, quarantines the previous lane and analytics files, and writes an
operator-visible `planned`/`completed` audit. Any interrupted or malformed audit keeps services
unready.

Dry-runs never require a stop and never write:

```bash
docker compose --env-file .env.deploy --profile ops run --rm backup \
  backup owner/name /var/lib/issue-graphrag/backups/2026-08-26 --dry-run
docker compose --env-file .env.deploy --profile ops run --rm backup \
  restore owner/name /var/lib/issue-graphrag/backups/2026-08-26 --dry-run
```

An actual snapshot or restore requires all data writers and the Viewer to be stopped, plus an
explicit acknowledgement. Use a new backup directory each time:

```bash
docker compose --env-file .env.deploy --profile live stop proxy viewer sync worker receiver
docker compose --env-file .env.deploy --profile ops run --rm backup \
  backup owner/name /var/lib/issue-graphrag/backups/2026-08-26 \
  --confirm-services-stopped

docker compose --env-file .env.deploy --profile ops run --rm backup \
  restore owner/name /var/lib/issue-graphrag/backups/2026-08-26 \
  --confirm-services-stopped --confirm-repo owner/name
docker compose --env-file .env.deploy --profile live up -d receiver worker sync viewer proxy
```

Keep the restore quarantine and audit until state, queue, analytics and checkpoint status are
verified. A restore does not delete them automatically.

## CI evidence and remaining pre-apply gate

`scripts/compose_smoke.sh` renders and validates Compose, builds the image, starts the public path,
durably accepts a signed fixture while the worker is stopped, restarts the receiver, proves the
worker processes the retained delivery, separates liveness from a read-only-volume readiness
failure, performs an offline backup/restore, and starts from the restored state. GitHub Actions
runs this on every PR because the current development host does not provide Docker.

This evidence is necessary but not sufficient for a real deployment. Before apply, the reviewed
package must still name the provider/account/region, instance and volume, TLS hostname, rate and
connection limits, minimum GitHub permissions, whether an OpenRouter key is deployed, current and
worst-case cost, rollback, teardown and post-destroy retention. No command in this foundation
creates, updates or destroys a cloud resource.
