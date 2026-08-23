# Repository isolation and bounded bootstrap

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
    freshness.json
```

The inbox, state, event log and extraction results are never shared between repositories. One
receiver/worker pair still owns one repository lane; start another pair for another repository.
`llm_operations.sqlite` is deliberately shared: it contains only provider request reservations and
actual usage metadata, and serializes the all-repository daily/monthly hard caps. Every ledger row
is repo-qualified; it contains no extracted result or source text.

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

## Freshness semantics

Each lane's `freshness.json` reports separate source and semantic clocks:

| Field | Meaning |
|---|---|
| `last_source_sync_at` | Last successful GitHub bootstrap fetch |
| `last_state_commit_at` | Last atomic live-state write |
| `semantic_status` | `not_started`, `pending`, `current` or `degraded` |
| `semantic_updated_at` | Time the semantic status was last updated |
| `last_error` | Most recent lane-local processing error |

Fetching a seed marks semantic work `pending`. Deterministic replay can commit immediately; an
LLM-enabled worker later materializes one durable semantic job per changed document. Only a fully
cached and validated document marks its extraction signature current. Provider/quota failure marks
only that repository `degraded`, preserves last-good semantic facts and leaves the job resumable.
The Streamlit live tab shows these fields next to the repository selector.

Use the repository-qualified CLI paths when more than one repository is registered:

```bash
python scripts/contribution_report.py --repo owner/name
python scripts/process_webhooks.py --repo owner/name --status
```

`--status` is read-only with respect to the repository registry and reports delivery failures,
semantic cursors, validated cache units and global ledger request states. Processing failures and
retry queues remain local to their lane, so a degraded repository does not block a healthy one.
