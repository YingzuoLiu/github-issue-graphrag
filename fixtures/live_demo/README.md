# Live contribution graph demo fixtures

Everything in this directory is **synthetic and hand-authored**. Issue numbers and technical themes
mirror the batch demo (Kafka backend teardown, graph-rag latency, hybrid retrieval) so both halves
of the project tell the same story, but no file here is a captured GitHub payload or a recorded
model response.

| File | What it is |
|---|---|
| `seed.json` | The repository snapshot the index is bootstrapped from: four open issues. |
| `events/*.json` | Delivery envelopes, replayed in filename order. Each carries its own `X-GitHub-Delivery` id and `received_at`, which is what makes replay deterministic. |
| `extraction_rules.json` | Substring rules that stand in for LLM extraction. |

## Why a rule-based extractor

The incremental behaviour worth testing — scoped re-extraction, fact invalidation when a comment is
deleted, provenance on every inferred edge, replay equalling rebuild — has nothing to do with how
good the extraction is. Making the extraction step deterministic and offline means those properties
can be asserted in CI without an API key or a per-run bill.

`FixtureExtractor` and `LLMExtractor` implement the same `Extractor` protocol, so the pipeline path
is identical. Pass `--llm` to `scripts/replay_events.py` to use the configured provider instead.

One rule (`poll loop no longer holds`) deliberately emits a `closes` relation. `closes` is a
predicate only GitHub may assert, so the ontology rejects it and the replay reports the rejection.
It is there to prove the guard rail fires.

## The story the events tell

1. `001` — a comment on #922 proposes a direct Bolt traversal. New inferred concepts appear.
2. `002` — PR #950 opens with `Fixes #944` and a file list. #944 becomes **claimed**.
3. `003` — a drive-by comment on #875 suggests Elasticsearch. Its score rises.
4. `004` — that comment is deleted. The inferred facts are **invalidated, not erased**.
5. `005` — PR #950 merges.
6. `006` — #944 closes. It leaves the ranking, and #901 is **unblocked**.
7. `007` — GitHub redelivers `d-0002`. Nothing changes.
