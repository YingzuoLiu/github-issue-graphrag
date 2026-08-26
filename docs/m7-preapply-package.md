# M7 AWS Lightsail pre-apply package

Status: **example rendered; no cloud mutation; not approval-ready**  
Pricing checked: **2026-08-26**

This package turns the provider-neutral operations foundation into a concrete, reviewable public
deployment proposal. It intentionally stops before using an AWS credential, changing DNS,
registering a GitHub webhook, or creating any resource.

## Problem, existing solution and remaining gap

The product already has deterministic receiver, worker, synchronizer, Viewer, health/readiness,
backup/restore and Compose boundaries. CI proves those processes can restart and recover without
losing a signed webhook. That solves process isolation, but not public deployment: local Caddy has
no approved hostname or trusted certificate and does not provide the required connection and rate
limits; no provider resource, disk, firewall or teardown plan is named.

The pre-apply renderer closes that value gap without delegating a security or cost decision to an
LLM. A checked JSON input is transformed into a canonical plan, a Traefik routing file and a
non-secret Compose environment file. Unsafe changes such as public Ask, an OpenRouter credential,
SSH from the whole internet, an unpinned proxy image or a claimed AWS hard ceiling are rejected.

## Recommended single-host architecture

```text
Internet
   |
   | TCP 80/443 only
   v
Lightsail static IP -> public-proxy (Traefik, no Docker socket, no credentials)
                           |                         |
                           | /webhooks/github       | all other approved-host paths
                           v                         v
                    receiver :8000             viewer :8501
                    webhook secret             zero credentials
                           |                         |
                           +------ durable disk ----+
                                      ^
                                      |
                              worker + synchronizer
                              read-only GitHub token
```

The proposed host is one AWS Lightsail Linux instance in Singapore, with one attached 20 GiB block
disk. The app services run as UID 10001 with read-only root filesystems. Repository state, inbox,
checkpoint, cache, analytics, application backups and ACME state live under
`/srv/issue-graphrag`; secret files live separately under `/etc/issue-graphrag/secrets` on the
instance disk. The Viewer sees repository data read-only and analytics separately read-write.

Public Ask remains disabled and no OpenRouter key is deployed. Internal ports 8000, 8501 and 8082
are never published. SSH is restricted to one or more operator-approved CIDRs; the example uses an
IANA documentation address and cannot authorize apply.

## Provider comparison and recommendation

| Option | Comparable shape | Persistent storage | Current monthly basis | Decision |
|---|---|---|---|---|
| AWS Lightsail Singapore | 2 vCPU, 2 GiB, 60 GB instance SSD, 3 TB transfer | separate 20 GiB disk, daily automatic snapshots | `$12` instance + `$2` disk + snapshot bytes | recommended |
| DigitalOcean Singapore | 2 vCPU, 2 GiB Droplet | volume pricing shown from 100 GiB | `$18` Droplet + `$10` volume before snapshots/backups | rejected for this MVP |

Primary sources:

- [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/) lists the public-IPv4 2 GiB / 2
  vCPU bundle at `$12/month`, including 60 GB SSD and 3 TB transfer; block storage is
  `$0.10/GB-month`, and snapshots are `$0.05/GB-month`.
- [Lightsail regions](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-regions-and-availability-zones-in-amazon-lightsail.html)
  includes Singapore `ap-southeast-1`.
- [Lightsail automatic snapshots](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-configuring-automatic-snapshots.html)
  are daily, retain the latest seven, and are deleted with the source resource unless copied to a
  manual snapshot.
- [DigitalOcean Droplet pricing](https://www.digitalocean.com/pricing/droplets) lists the comparable
  2 GiB / 2 vCPU Droplet at `$18/month`; [volume pricing](https://www.digitalocean.com/pricing/volumes)
  starts the displayed general-purpose volume at 100 GiB / `$10/month`.

The proposed fixed compute and disk cost is `$14/month`. With about 20 GiB of stored snapshot data,
the working estimate is `$15/month`. A deliberately pessimistic storage scenario in which each of
the seven retained snapshots stores another full 20 GiB is `$21/month` before egress. Snapshot
storage is incremental in normal use, but the plan does not pretend that changed bytes are known
before deployment.

The instance includes 3 TB transfer. Singapore transfer beyond the allowance is currently
`$0.12/GB`, so no finite worst-case bill exists while the endpoint remains public. The proposal
creates a free monitoring-only AWS Budget at `$20` with 80% and 100% email alerts. AWS documents
that [budget data and notifications can be delayed](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html),
so this is **not a hard stop**. If the selected account exposes a project spend-limit feature, that
can be evaluated separately during operational review; it is not assumed here.

Domain registration, OpenRouter, taxes and overage are excluded from the estimate.

## Named resources in the proposal

| Kind | Name / value | Lifecycle |
|---|---|---|
| Lightsail instance | `issue-graphrag-prod` | create after approval; replaceable |
| Bundle / OS | `small_3_0` / `ubuntu_24_04` | re-verify active IDs during read-only preflight |
| Region / AZ | `ap-southeast-1` / `ap-southeast-1a` | requires account approval |
| Static IPv4 | `issue-graphrag-prod-ip` | attach to instance; release on teardown |
| Data disk | `issue-graphrag-prod-data`, 20 GiB | durable; final manual snapshot before deletion |
| Automatic snapshots | daily at `18:00` UTC, seven retained | source-bound; copied before teardown |
| AWS Budget | `issue-graphrag-prod-monthly`, `$20` alert | account-level notification, not a stop |
| Public hostname | `radar.example.com` in the example | must be replaced with an approved real FQDN |
| TLS | Let's Encrypt HTTP-01 | ACME state persisted on data disk |

The DNS A record is not created by this repository because the domain/provider is still a user
decision. No load balancer, database, S3 bucket, Kubernetes cluster or LLM service is proposed.

## Public edge contract

`compose.public.yaml` adds a `public-proxy` service in the explicit `public` profile. The local
Caddy service is confined to the `local` profile, so a production startup cannot launch it by
accident. The public proxy:

- pins `traefik:v3.6.14`, a version containing the April 2026 BasicAuth timing fix documented in
  [GHSA-6x2q-h3cr-8j2h](https://github.com/traefik/traefik/security/advisories/GHSA-6x2q-h3cr-8j2h);
- uses only the file provider and receives no Docker socket;
- receives no Compose secret or application credential;
- publishes only TCP 80 and 443;
- stores ACME state on the durable disk and uses HTTP-01 on port 80; Traefik documents automatic
  renewal and the port-80 requirement in its [ACME reference](https://doc.traefik.io/traefik/reference/install-configuration/tls/certificate-resolvers/acme/);
- drops all request headers from access logs by default, which excludes the GitHub signature;
- sends HSTS, frame-deny, no-sniff, referrer and permissions-policy headers.

The generated dynamic configuration has separate controls for the webhook and Viewer:

| Lane | Body limit | Concurrent in-flight requests | Token-bucket rate |
|---|---:|---:|---:|
| signed webhook | 25 MiB | 8 | 1 request/second average, burst 20 |
| public Viewer | 1 MiB | 32 | 2 requests/second average, burst 20 |

These use Traefik's documented [Buffering](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/buffering/),
[InFlightReq](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/inflightreq/)
and [RateLimit](https://doc.traefik.io/traefik/reference/routing-configuration/http/middlewares/ratelimit/)
middlewares. Because Traefik is the internet-facing process rather than being behind another
proxy, no forwarded-header source is trusted.

## Secrets and GitHub boundary

Only secret **names and paths** appear in the plan:

| Compose secret | Host path | Consumers |
|---|---|---|
| `github_token` | `/etc/issue-graphrag/secrets/github_token` | worker, synchronizer |
| `github_webhook_secret` | `/etc/issue-graphrag/secrets/github_webhook_secret` | receiver |

The GitHub credential should be a fine-grained token restricted to the one approved repository,
with Metadata read, Issues read and Pull requests read only. The relevant GitHub API endpoints are
listed in GitHub's [fine-grained token permission reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).
The Viewer and both proxies receive neither token. Application GitHub traffic uses the existing
`CountingSession`, which refuses POST, PUT, PATCH and DELETE before network dispatch.

After the public endpoint passes preflight and only after apply approval, the owned repository
webhook must subscribe to `issues`, `issue_comment`, `pull_request` and `issue_dependencies`, point
to `https://<approved-host>/webhooks/github`, use the file-backed HMAC secret, and enable SSL
verification. GitHub documents those event names and payloads in its
[webhook event reference](https://docs.github.com/en/webhooks/webhook-events-and-payloads).

## Deterministic local render

Render the committed, deliberately non-authorizing example:

```bash
python scripts/render_preapply_plan.py \
  deploy/aws-lightsail/preapply.example.json \
  --output-dir /tmp/issue-graphrag-preapply
```

The output contains:

- `preapply-plan.json`: resources, commands, limits, costs, rollback and teardown;
- `traefik-dynamic.toml`: exact hostname routes and edge controls;
- `compose-public.env`: non-secret values and secret **file paths**, never secret values.

The example must print `"approval_ready": false`. To prepare an actual review package, copy the
example into ignored `deploy/aws-lightsail/rendered/input.json`, replace the documentation-only
account, repository, image tag, hostname, email and CIDR, and leave all approval booleans false
until the user has actually decided them. Render to a sibling ignored directory.

Validate the merged Compose model without starting a public endpoint:

```bash
docker compose \
  --env-file /tmp/issue-graphrag-preapply/compose-public.env \
  -f compose.yaml -f compose.public.yaml \
  --profile local --profile live --profile ops --profile public \
  config --format json > /tmp/issue-graphrag-compose-public.json
python scripts/check_compose_contract.py \
  --public /tmp/issue-graphrag-compose-public.json
```

CI performs both renders, validates the public contract, and then runs the existing local restart,
readiness-failure and backup/restore smoke. It does not start Traefik ACME or contact AWS.

Lightsail does not expose a transactional Terraform-style plan for these CLI operations. The
generated mutating command arrays are evidence for review, not commands this renderer executes.
Immediately before operational review, an operator with the selected read-only AWS identity must
capture the generated `read_only_preflight_commands`: caller identity, regions/AZs, active bundles
and active blueprints. A mismatch refuses the apply rather than silently choosing another product.

## Host bootstrap after approval only

The operational plan will perform these steps only after independent review and explicit user
approval:

1. Create the named instance, static IP, disk, restricted firewall and budget alert using the
   generated command arrays; wait for every operation to reach a terminal success state.
2. Format the new disk once, mount it at `/srv/issue-graphrag`, record it in `/etc/fstab` by UUID,
   create `repos`, `analytics`, `backups` and `traefik`, and give app data to UID/GID 10001.
3. Create `/etc/issue-graphrag/secrets` on the instance disk with mode 0700 and inject the two
   mode-0400 files through an approved operator channel. Never copy them into the data snapshot.
4. Copy the reviewed dynamic TOML to `/etc/issue-graphrag/traefik/dynamic.toml`; copy the rendered
   env file to a root-readable operations path; build/tag the app image with the exact reviewed Git
   SHA and record its image ID.
5. Point the approved DNS A record at the static IP. Confirm propagation before starting ACME.
6. Start only `receiver worker sync viewer public-proxy` with profiles `live` and `public`; do not
   start the `local` profile.
7. Verify TLS, headers, limits, Viewer credential absence, liveness/readiness, operator status and
   GitHub zero-write evidence before creating the owned webhook.
8. Create the webhook, run E2E-01 through E2E-05, then create and verify an application backup
   before the first provider snapshot window.

## Rollback, host replacement and teardown

Code rollback stops the public proxy and writers, restores the last verified application backup if
state changed incompatibly, and starts the previous exact app image tag. The public endpoint stays
closed until liveness, readiness, queue/checkpoint status and a read-only sync all pass.

Host replacement creates a new instance in the same AZ, attaches a disk restored from the selected
snapshot, re-injects secrets from their original authority, recreates the same mount paths, starts
the previous image, verifies readiness, and only then moves the static IP. Secret files are not
expected to be recoverable from the data snapshot.

Teardown is deliberately ordered:

1. stop all services;
2. create and verify a final application backup;
3. wait for a disk snapshot and copy it to a named manual snapshot;
4. prove the manual snapshot is available;
5. remove the GitHub webhook and DNS record;
6. detach/release the static IP, then delete instance and disk;
7. retain the manual snapshot for the user-approved period (proposal: 30 days);
8. delete the manual snapshot and AWS Budget only with a second explicit confirmation.

Deleting the disk destroys live repository state and analytics. Deleting the source disk also
deletes its automatic snapshots; only the copied manual snapshot survives. Deleting the final
manual snapshot is the irreversible data-destruction step.

## Current blockers before operational review and apply

The checked example cannot satisfy the gate. The user must still provide or confirm:

1. the exact AWS account ID/alias and that an account-wide `$20` budget alert is acceptable;
2. the real public hostname/domain and DNS authority;
3. the operator SSH CIDR and budget/ACME email;
4. the exact owned GitHub repository;
5. the 30-day post-destroy manual-snapshot retention proposal;
6. acceptance that `$20` is an alert, not a guaranteed provider stop, or a different approved
   cost-control mechanism and threshold.

After those values are rendered, the package still requires an independent operational plan
review. Only a passing review plus a new explicit user instruction authorizes cloud create/update/
destroy, DNS changes or webhook creation. Version `0.4.0`, tag `v0.4.0` and the release remain out
of scope until real E2E evidence and the cumulative blind MR pass.
