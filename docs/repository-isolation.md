# Repository isolation, bounded bootstrap and scheduled reconciliation

The live index keeps each repository in an independent local lane. This avoids the two failure
modes of the earlier single-path prototype: one repository overwriting another's state, and one
failed semantic update obscuring the health of every repository.

## Boundary and design rationale

`LiveState` already models exactly one repository, and the webhook receiver already allowlists one
repository per process. The missing boundary was storage: state, event log and inbox defaulted to
shared paths. Bootstrap also stopped after one page of comments and changed files, so a snapshot
could look complete while silently omitting older evidence.

M2 preserves the single-repository state model and makes that boundary explicit on disk. A small
registry powers the Streamlit selector and CLI defaults; it is not a cross-repository graph or a
distributed scheduler.

## Layout

Set repositories in `.env`:

```dotenv
GITHUB_REPOS=owner/name,other/repository
REPO_DATA_DIR=data/repos
```

Commands register repositories as they are bootstrapped or served. Names are validated,
case-normalized and mapped to paths without accepting traversal components:

```text
data/repos/
  repositories.json
  llm_operations.sqlite
  owner__name/
    bootstrap_seed.json
    inbox.db
    live_state.json
    event_log.jsonl
    extraction_cache.sqlite
    sync_state.json
    freshness.json
```

The inbox, state, event log, reconciliation checkpoint and extraction results are never shared
between repositories. One receiver/worker pair still owns one repository lane; start another pair
for another repository.
`llm_operations.sqlite` is deliberately shared: it contains only provider request reservations and
actual usage metadata, and serializes the all-repository daily/monthly hard caps. Every ledger row
is repo-qualified; it contains no extracted result or source text. Reservations are marked
dispatched before the provider boundary. Expired undispatched rows are released; expired dispatched
rows become conservative `unknown` outcomes. Incomplete provider usage keeps the reservation's
token and cost estimates, so missing metadata cannot make a hard cap fail open. Orphan reconciliation
has its own committed transaction before admission, and the status view reconciles before reading;
a rejected reservation therefore cannot roll back the operator-visible terminal state.

## Bounded bootstrap

Bootstrap follows GitHub pagination for the issue/PR list, comments and pull-request files. Every
network dimension has an operator-visible bound:

```bash
python scripts/fetch_live_seed.py owner/name \
  --state all \
  --limit 1000 \
  --item-max-pages 10 \
  --comment-limit-per-item 300 \
  --comment-max-pages 10 \
  --file-limit-per-pull 3000 \
  --file-max-pages 30
```

The selected bounds are stored in `bootstrap_seed.json` under `backfill`. Reducing a limit is an
explicit sampling decision rather than an undocumented first-page cutoff.

Replay the snapshot into its matching lane:

```bash
mkdir -p data/empty-events
python scripts/replay_events.py \
  --seed data/repos/owner__name/bootstrap_seed.json \
  --events data/empty-events
```

## Scheduled current-state reconciliation

The M5 synchronizer is a deterministic source observer, not a second indexer and not an LLM path.
It compares a bounded GitHub snapshot with the lane's last-good `sync_state.json`, gives each
changed resource a stable `reconciliation-<sha256>` delivery id, and enqueues
`source=reconciliation` events into the same durable inbox used by webhooks. The existing
single-writer worker remains the only component that commits live state, facts and event history.

Run one poll, inspect its checkpoint, or keep the fixed loop running:

```bash
python scripts/sync_repositories.py owner/name --once
python scripts/sync_repositories.py owner/name --status
python scripts/sync_repositories.py owner/name --loop --interval-seconds 900
python scripts/process_webhooks.py --repo owner/name
```

A bounded page this poll could not read to its end is not an observation. A truncated
`blocked_by` page never becomes a dependency count and a truncated file page never becomes a file
set, so an operator-configured bound can never publish a blocked issue as available or narrow a
pull request's modules; the previous value stays until a complete window replaces it. A first
complete observation is delivered like any other, which is what repairs a `blocked_by_removed`
webhook that never arrived.

The default cadence is 15 minutes (`GITHUB_SYNC_INTERVAL_SECONDS=900`). Requests are serial,
read-only and conditional when GitHub supplied `ETag` or `Last-Modified`; `X-Poll-Interval` may
extend the cadence. Retryable network/5xx failures use bounded exponential backoff. Rate-limit
responses do not sleep in-process: their `Retry-After` or `X-RateLimit-Reset` becomes the visible
next attempt, with a positive one-minute fallback for stale or zero hints. A failed observation or
partial enqueue never advances `sync_state.json`; the
last-good snapshot remains available, freshness becomes `stale`, and a retry uses the same
delivery ids. This converges current issue, pull request, comment, changed-file and dependency
state, but deliberately does not reconstruct GitHub's missed webhook chronology.

## Freshness semantics

Each lane's `freshness.json` reports separate source and semantic clocks:

| Field | Meaning |
|---|---|
| `last_source_sync_at` | Last successful bootstrap or scheduled observation |
| `last_source_attempt_at` | Most recent source attempt, successful or failed |
| `next_source_sync_at` | Earliest scheduled next observation |
| `source_status` | `not_started`, `current` or `stale` |
| `source_kind` | `bootstrap` or `scheduled_sync` |
| `source_error` | Latest source-side failure while last-good state stays readable |
| `last_source_requests` | GitHub GETs made by the latest successful scheduled observation |
| `last_source_not_modified` | Conditional requests answered `304` in that observation |
| `last_source_deliveries` | Reconciliation deliveries planned in that observation |
| `last_state_commit_at` | Last atomic live-state write |
| `semantic_status` | `not_started`, `pending`, `current` or `degraded` |
| `semantic_updated_at` | Time the semantic status was last updated |
| `last_error` | Most recent lane-local processing error |

Fetching a seed marks semantic work `pending`. Deterministic replay can commit immediately; an
LLM-enabled worker later materializes one durable semantic job per changed document. Only a fully
cached and validated document marks its extraction signature current. Provider/quota failure marks
only that repository `degraded`, preserves last-good semantic facts and leaves the job resumable.
Operational freshness also records the extraction namespace. Model, prompt, schema, strict request,
per-call output or live chunk-policy changes therefore queue unchanged documents into the new
namespace instead of leaving old facts marked current.
The Streamlit live tab shows these fields next to the repository selector.

Use the repository-qualified CLI paths when more than one repository is registered:

```bash
python scripts/contribution_report.py --repo owner/name
python scripts/process_webhooks.py --repo owner/name --status
```

`--status` is read-only with respect to the repository registry and reports delivery failures,
semantic cursors, validated cache units and global ledger request states. Processing failures and
retry queues remain local to their lane, so a degraded repository does not block a healthy one.
