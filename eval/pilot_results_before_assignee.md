# Real-repository contribution pilot

Pilot 0 is a read-only engineering evaluation, not a user study. It tests contradictions
against explicit GitHub facts and two inspection-burden proxies. It does **not** prove that
contributors work faster or that maintainers perceive less burden.

## Summary

| repository | issues | PRs | API GETs | false available | product P@10 | recent P@10 | curated P@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| [getzep/graphiti](https://github.com/getzep/graphiti) | 50 | 50 | 3 | 3 (13.0%) | 80.0% | 40.0% | 40.0% |
| [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | 50 | 50 | 4 | 34 (85.0%) | 0.0% | 10.0% | 60.0% |
| [trustgraph-ai/trustgraph](https://github.com/trustgraph-ai/trustgraph) | 17 | 2 | 2 | 2 (12.5%) | 80.0% | 90.0% | 100.0% |

The curated baseline uses only native GitHub fields: no assignee, newcomer labels first,
then recent update order. The actionability oracle excludes issues with an assignee, a
locked conversation, a GitHub dependency, or an open PR using a closing keyword.

## getzep/graphiti

Snapshot: `2026-08-22T07:39:18Z`; fingerprint `9c59e4a942739f3d5530b6d848b16755cea4f028a7abbfd724b28593f1d116ba`.
GitHub operations: 3 GET, **0 writes**.

| ranking | candidates | precision | inspections for 3 actionable |
|---|---:|---:|---:|
| product available | 23 | 80.0% | 3 |
| GitHub recent | 50 | 40.0% | 9 |
| GitHub curated | 47 | 40.0% | 9 |

Precommitted false-available threshold: 5.0%; result **FAIL**.

False-available examples:

- [#1425 [BUG] hypens "-" in title silently fails with syntax error while processing episode from the graphiti mcp server](https://github.com/getzep/graphiti/issues/1425): assigned to paul-paliychuk.
- [#1469 [BUG] add_episode crashes with Kuzu C-extension access violation on Windows (kuzu 0.11.3, graphiti-core 0.29.0)](https://github.com/getzep/graphiti/issues/1469): assigned to prasmussen15.
- [#1600 Optimisation - ACO in node_operations.py](https://github.com/getzep/graphiti/issues/1600): assigned to vedantRaikar.

Plain-reference claims requiring human review (reported separately, not scored as
ground-truth unavailable):

- [#1645 MCP server: search behavior is hardcoded — proposal: configurable reranker, stale-fact filtering, result counts, relevance scores](https://github.com/getzep/graphiti/issues/1645): #1790.
- [#1714 RFC: Optional OpenTelemetry metrics + span instrumentation for core memory operations](https://github.com/getzep/graphiti/issues/1714): #1718.
- [#1723 Anthropic path never emits cache_control, and MODEL_EFFORT does not exist — the "cheap Opus graph extraction" recipe silently bills at full input rate](https://github.com/getzep/graphiti/issues/1723): #1753.
- [#1727 CLA signatures in the bot's own requested format are silently discarded, blocking 18 open PRs](https://github.com/getzep/graphiti/issues/1727): #1730.
- [#1751 OpenAI models rejected with 400 `invalid_json_schema` on OpenRouter](https://github.com/getzep/graphiti/issues/1751): #1752.

## pydantic/pydantic-ai

Snapshot: `2026-08-22T07:39:24Z`; fingerprint `ff36d33b6a02801ecb350b3eb84f841cdb914ae9ff0cf1e48f7b16fe4a2461d9`.
GitHub operations: 4 GET, **0 writes**.

| ranking | candidates | precision | inspections for 3 actionable |
|---|---:|---:|---:|
| product available | 40 | 0.0% | 37 |
| GitHub recent | 50 | 10.0% | 37 |
| GitHub curated | 15 | 60.0% | 6 |

Precommitted false-available threshold: 5.0%; result **FAIL**.

False-available examples:

- [#1270 MCP improvement: CLI support](https://github.com/pydantic/pydantic-ai/issues/1270): assigned to samuelcolvin.
- [#1275 type check docs examples](https://github.com/pydantic/pydantic-ai/issues/1275): assigned to samuelcolvin.
- [#1590 Structured Output fails with text output + Behaviour inconsistency](https://github.com/pydantic/pydantic-ai/issues/1590): assigned to DouweM, adtyavrdhn.
- [#2330 Support MCP elicitations both in client and server](https://github.com/pydantic/pydantic-ai/issues/2330): assigned to DouweM.
- [#2472 Let OTel messages/events from Logfire be deserialized into `ModelMessage`s](https://github.com/pydantic/pydantic-ai/issues/2472): assigned to adtyavrdhn, dmontagu.

Plain-reference claims requiring human review (reported separately, not scored as
ground-truth unavailable):

- [#7683 Test files importing `snapshot` from `inline_snapshot` instead of the `tests._inline_snapshot` wrapper fail locally with an internal `UsageError`](https://github.com/pydantic/pydantic-ai/issues/7683): #7681.

## trustgraph-ai/trustgraph

Snapshot: `2026-08-22T07:39:26Z`; fingerprint `7795e6ce55d2a647188fa98d75dbc9f688c9d420d31b0b404ce989d606fa2ac0`.
GitHub operations: 2 GET, **0 writes**.

| ranking | candidates | precision | inspections for 3 actionable |
|---|---:|---:|---:|
| product available | 16 | 80.0% | 3 |
| GitHub recent | 17 | 90.0% | 3 |
| GitHub curated | 15 | 100.0% | 3 |

Precommitted false-available threshold: 5.0%; result **FAIL**.

False-available examples:

- [#243 Feature/Bug: Add a pdf filetype check on processing](https://github.com/trustgraph-ai/trustgraph/issues/243): assigned to cybermaggedon.
- [#299 Support for Qdrant API key](https://github.com/trustgraph-ai/trustgraph/issues/299): assigned to cybermaggedon.

Plain-reference claims requiring human review (reported separately, not scored as
ground-truth unavailable):

- [#783 Replace bare except: with specific exceptions](https://github.com/trustgraph-ai/trustgraph/issues/783): #1097.

## What remains unproven

- Time-to-selection needs a timed A/B task with contributors; inspection depth is only a proxy.
- Maintainer burden needs maintainer feedback; this run proves only that repository writes are zero.
- The snapshot samples recent open items and the latest repository-wide comments, so it can miss
  old comments or PRs outside the sample. A GitHub App backfill is not part of Pilot 0.
- A plain PR reference is ambiguous. Only closing keywords count as oracle evidence; ambiguous
  references are listed for manual review instead of being declared right or wrong.
- Pilot 0 uses the deterministic GitHub layer only. Semantic fit and personalization require a
  separate evaluation after factual availability is reliable.
