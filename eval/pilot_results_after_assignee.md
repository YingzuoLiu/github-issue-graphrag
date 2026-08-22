# Real-repository contribution pilot

Pilot 0 is a read-only engineering evaluation, not a user study. It tests contradictions
against explicit GitHub facts and two inspection-burden proxies. It does **not** prove that
contributors work faster or that maintainers perceive less burden.

## Summary

| repository | issues | PRs | API GETs | false available | product P@10 | actionable coverage | recent P@10 | curated P@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| [getzep/graphiti](https://github.com/getzep/graphiti) | 50 | 50 | 3 | 0 (0.0%) | 100.0% | 80.0% | 40.0% | 40.0% |
| [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | 50 | 50 | 4 | 0 (0.0%) | 100.0% | 75.0% | 10.0% | 60.0% |
| [trustgraph-ai/trustgraph](https://github.com/trustgraph-ai/trustgraph) | 17 | 2 | 2 | 0 (0.0%) | 100.0% | 93.3% | 90.0% | 100.0% |

The curated baseline uses only native GitHub fields: no assignee, newcomer labels first,
then recent update order. The actionability oracle excludes issues with an assignee, a
locked conversation, a GitHub dependency, or an open PR using a closing keyword.

## getzep/graphiti

Snapshot: `2026-08-22T07:54:49Z`; fingerprint `b74ccfc9668c4223be15d4d3543f4712835e04880657e4a58306b0519691fd9f`.
GitHub operations: 3 GET, **0 writes**.

| ranking | candidates | precision | inspections for 3 actionable |
|---|---:|---:|---:|
| product available | 20 | 100.0% | 3 |
| GitHub recent | 50 | 40.0% | 9 |
| GitHub curated | 47 | 40.0% | 9 |

Precommitted false-available threshold: 5.0%; result **PASS**.

Assignee-fact ablation on this exact snapshot:

| treatment | available candidates | false available | precision | inspections for 3 actionable |
|---|---:|---:|---:|---:|
| assignee facts suppressed | 23 | 3 (13.0%) | 80.0% | 3 |
| current graph | 20 | 0 (0.0%) | 100.0% | 3 |

Oracle-actionable items withheld by conservative graph signals:

- [#1645 MCP server: search behavior is hardcoded — proposal: configurable reranker, stale-fact filtering, result counts, relevance scores](https://github.com/getzep/graphiti/issues/1645): open issue (+1.00); already picked up by PR #1790 (open) (-2.00).
- [#1714 RFC: Optional OpenTelemetry metrics + span instrumentation for core memory operations](https://github.com/getzep/graphiti/issues/1714): open issue (+1.00); already picked up by PR #1718 (open) (-2.00).
- [#1723 Anthropic path never emits cache_control, and MODEL_EFFORT does not exist — the "cheap Opus graph extraction" recipe silently bills at full input rate](https://github.com/getzep/graphiti/issues/1723): open issue (+1.00); already picked up by PR #1753 (open) (-2.00).
- [#1727 CLA signatures in the bot's own requested format are silently discarded, blocking 18 open PRs](https://github.com/getzep/graphiti/issues/1727): open issue (+1.00); already picked up by PR #1718 (open), PR #1726 (open), PR #1730 (draft) (-2.00).
- [#1751 OpenAI models rejected with 400 `invalid_json_schema` on OpenRouter](https://github.com/getzep/graphiti/issues/1751): open issue (+1.00); already picked up by PR #1752 (open) (-2.00).

These are not automatically counted as product errors: the explicit oracle may
be incomplete. They are the required human-review set for the next pilot stage.

## pydantic/pydantic-ai

Snapshot: `2026-08-22T07:54:55Z`; fingerprint `d7ae5dd5c8632fbbce7502fe32f2fe2be36178a4c26267d510e6aab2fec4a399`.
GitHub operations: 4 GET, **0 writes**.

| ranking | candidates | precision | inspections for 3 actionable |
|---|---:|---:|---:|
| product available | 6 | 100.0% | 3 |
| GitHub recent | 50 | 10.0% | 37 |
| GitHub curated | 15 | 60.0% | 6 |

Precommitted false-available threshold: 5.0%; result **PASS**.

Assignee-fact ablation on this exact snapshot:

| treatment | available candidates | false available | precision | inspections for 3 actionable |
|---|---:|---:|---:|---:|
| assignee facts suppressed | 40 | 34 (85.0%) | 0.0% | 37 |
| current graph | 6 | 0 (0.0%) | 100.0% | 3 |

Oracle-actionable items withheld by conservative graph signals:

- [#7648 Add multimodal `ToolReturn.content` durability coverage for DBOS and Prefect](https://github.com/pydantic/pydantic-ai/issues/7648): open issue (+1.00); already picked up by PR #7618 (open) (-2.00).
- [#7683 Test files importing `snapshot` from `inline_snapshot` instead of the `tests._inline_snapshot` wrapper fail locally with an internal `UsageError`](https://github.com/pydantic/pydantic-ai/issues/7683): open issue (+1.00); already picked up by PR #7681 (open) (-2.00).

These are not automatically counted as product errors: the explicit oracle may
be incomplete. They are the required human-review set for the next pilot stage.

## trustgraph-ai/trustgraph

Snapshot: `2026-08-22T07:54:56Z`; fingerprint `c20e9f8383ca3cc673290f98fa13faa55224ced245b352e659dd1ebe6d7c8899`.
GitHub operations: 2 GET, **0 writes**.

| ranking | candidates | precision | inspections for 3 actionable |
|---|---:|---:|---:|
| product available | 14 | 100.0% | 3 |
| GitHub recent | 17 | 90.0% | 3 |
| GitHub curated | 15 | 100.0% | 3 |

Precommitted false-available threshold: 5.0%; result **PASS**.

Assignee-fact ablation on this exact snapshot:

| treatment | available candidates | false available | precision | inspections for 3 actionable |
|---|---:|---:|---:|---:|
| assignee facts suppressed | 16 | 2 (12.5%) | 80.0% | 3 |
| current graph | 14 | 0 (0.0%) | 100.0% | 3 |

Oracle-actionable items withheld by conservative graph signals:

- [#783 Replace bare except: with specific exceptions](https://github.com/trustgraph-ai/trustgraph/issues/783): open issue (+1.00); labeled good first issue (+0.75); already picked up by PR #1097 (open) (-2.00).

These are not automatically counted as product errors: the explicit oracle may
be incomplete. They are the required human-review set for the next pilot stage.

## What remains unproven

- Time-to-selection needs a timed A/B task with contributors; inspection depth is only a proxy.
- Maintainer burden needs maintainer feedback; this run proves only that repository writes are zero.
- The snapshot samples recent open items and the latest repository-wide comments, so it can miss
  old comments or PRs outside the sample. A GitHub App backfill is not part of Pilot 0.
- A plain PR reference is ambiguous. Only closing keywords count as oracle evidence; ambiguous
  references are listed for manual review instead of being declared right or wrong.
- Pilot 0 uses the deterministic GitHub layer only. Semantic fit and personalization require a
  separate evaluation after factual availability is reliable.
