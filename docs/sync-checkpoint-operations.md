# Scheduled-sync checkpoint operations

This runbook covers the deterministic checkpoint used by
`scripts/sync_repositories.py`. It does not authorize cloud changes and it does not alter GitHub.

## Problem, existing protection and remaining gap

The scheduled synchronizer observes only a bounded, recently updated GitHub window. Its
`sync_state.json` remembers earlier fingerprints so the next complete observation can identify
changes and missing comments. Version 1 already wrote the primary atomically and never advanced it
after a failed observation or partial enqueue.

Two properties were still missing:

1. Retaining every resource ever observed made the file and each poll rewrite grow without a hard
   bound.
2. Invalid JSON or a fingerprint mismatch made every poll and the old `--status` path fail, with
   no explicit recovery state machine.

The checkpoint remains a deterministic control document. The LLM does not select retention,
infer deletion, decide recovery or write any of these files.

## Why checkpoint v2 uses manifests plus family compaction

| Option | Result |
|---|---|
| Delete old comment fingerprints individually | Rejected: a later complete page could no longer prove which old comment disappeared. |
| Keep compact per-comment tombstones forever | Rejected as the sole strategy: payloads shrink, but cardinality still grows without a bound. |
| Treat absence in every bounded page as deletion | Rejected: a truncated page is not complete evidence. |
| Complete comment manifest + whole-family compaction + hard ceiling | Selected: absence remains evidence-based, inactive state is removable, and growth fails closed at a known limit. |

A resource family is one issue or pull request plus its comments, dependency observation and
complete-comment manifest. A family seen in the current poll is never partially compacted. An
inactive closed family has a 30-day default retention; an inactive open family has a 90-day
default. These are operational defaults, not GitHub facts, and can be changed explicitly:

```bash
python scripts/sync_repositories.py owner/name --once \
  --checkpoint-closed-retention-seconds 2592000 \
  --checkpoint-open-retention-seconds 7776000
```

Only a page read to its end produces `comment_manifest:<kind>:<number>`. The manifest carries the
sorted current comment ids and a stable content fingerprint. The single writer removes only a
stored comment absent from that complete set; correctness is independent of whether the inbox
claims the aggregate or an included comment payload first. A newer webhook edit wins over an
older manifest by source time. An incomplete page produces no manifest and never infers deletion.

The defaults also stop before committing more than 12,000 resources or 64 MiB. Limits are checked
before any delivery from that poll is enqueued:

```bash
python scripts/sync_repositories.py owner/name --once \
  --checkpoint-max-resources 12000 \
  --checkpoint-max-bytes 67108864
```

Crossing either ceiling preserves the primary/last-good generation, marks source freshness stale
and returns a failed sync. Increase a limit only after inspecting why the active bounded window
needs it; do not delete fingerprints manually.

## Files and states

For `sync_state.json`, v2 keeps adjacent operational files:

| Path | Purpose |
|---|---|
| `sync_state.json` | Primary validated checkpoint. |
| `sync_state.last-good.json` | Previous verified generation; the first commit seeds it with the same valid generation. |
| `sync_state.quarantine/` | Exact pre-recovery primary bytes, including corrupt bytes. |
| `sync_state.recovery/` | Atomic per-operation audit records. `planned` means interruption; `completed` means post-write validation passed. |

`--status` never trusts the primary before reporting. It shows `missing`, `healthy` or `corrupt`,
on-disk version, bytes, resource/family/kind counts, hard ceilings, last-good health, quarantine
count, recovery-record count and any `planned` recovery that never reached `completed`. A corrupt
primary, a missing primary with retained last-good state, or a pending recovery prints a result
without a traceback and exits with code 2. A first-run `missing` state with no last-good file is
the only missing state that does not require recovery.

```bash
python scripts/sync_repositories.py owner/name --status
```

The poll loop never automatically restores, quarantines, deletes or rebaselines a corrupt or
unexpectedly missing checkpoint. It stops before GitHub observation, keeps the available evidence
and marks the repository source stale.

## Restore the verified last-good generation

Stop the synchronizer loop for this repository before recovery. First run the non-mutating plan:

```bash
python scripts/sync_repositories.py owner/name --recover-checkpoint --dry-run
```

Check the reported source, quarantine destination and warning. Then confirm the exact canonical
repository name:

```bash
python scripts/sync_repositories.py owner/name \
  --recover-checkpoint \
  --confirm-repo owner/name
```

The operation writes a `planned` audit record, copies the current primary bytes to quarantine,
atomically installs the verified last-good bytes, validates them again and marks the audit
`completed`. It can also roll back a healthy rebaseline while last-good still contains the prior
generation; the exact confirmation is still required.

After restore:

```bash
python scripts/sync_repositories.py owner/name --status
python scripts/sync_repositories.py owner/name --once
python scripts/process_webhooks.py --repo owner/name --once
python scripts/sync_repositories.py owner/name --status
```

Do not resume the loop until the primary is healthy and the one-shot poll/worker path succeeds.
Retries use deterministic delivery ids, so already accepted unchanged resources are inbox
duplicates rather than new facts.

## Operator-confirmed rebaseline

Use rebaseline only when no verified last-good generation is available or when an operator has
deliberately rejected it. Always inspect its warning first:

```bash
python scripts/sync_repositories.py owner/name --rebaseline-checkpoint --dry-run
```

Then, if the empty checkpoint is intentional:

```bash
python scripts/sync_repositories.py owner/name \
  --rebaseline-checkpoint \
  --confirm-repo owner/name
```

Rebaseline quarantines the prior bytes and installs an empty v2 primary. It does not clear the
durable inbox, live state, event log or facts. Therefore unchanged resource versions retain their
stable delivery ids and deduplicate. A changed complete comment manifest repairs comments deleted
while their family was absent. Until GitHub returns a complete comment page, deletion remains
unknown and the worker keeps last-good live state rather than producing a false delete.

If rebaseline produces an unacceptable result and a healthy last-good file existed, stop the loop
and run the restore procedure above before another successful checkpoint commit replaces that
backup generation. Quarantine and audit files are evidence; do not edit or remove them during the
incident.

## Validation evidence expected for changes

Checkpoint changes must retain tests for:

- v1-to-v2 migration without modifying the file during read;
- open versus closed retention and whole-family compaction;
- incomplete comment pages and post-compaction deletion inference;
- resource/byte ceilings before enqueue;
- long-cycle bounded growth under continuous churn;
- malformed JSON and valid-JSON fingerprint corruption;
- status without traceback, dry-run without writes and exact-repository confirmation;
- last-good restore, explicit rebaseline, quarantine/audit and rollback;
- an interrupted primary replace leaving the previous generation readable;
- full existing single-writer, temporal-ordering, repo-isolation and zero-GitHub-write regressions.
