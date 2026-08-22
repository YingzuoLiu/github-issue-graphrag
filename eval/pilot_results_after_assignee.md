# Real-repository contribution pilot

Pilot 0 is a read-only engineering consistency and coverage evaluation, not a user study
or an independent recommendation-quality benchmark. It does **not** prove that contributors
work faster or that maintainers perceive less burden.

## Summary

| repository | issues | PRs | API GETs | available ∩ constrained | product clear P@10 | clear coverage | recent clear P@10 | curated clear P@10 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| [getzep/graphiti](https://github.com/getzep/graphiti) | 50 | 50 | 3 | 0 (0.0%) | 100.0% | 80.0% | 40.0% | 40.0% |
| [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | 50 | 50 | 4 | 0 (0.0%) | 70.0% | 77.8% | 30.0% | 70.0% |
| [trustgraph-ai/trustgraph](https://github.com/trustgraph-ai/trustgraph) | 17 | 2 | 2 | 0 (0.0%) | 100.0% | 93.3% | 90.0% | 100.0% |

The curated baseline uses only native GitHub fields: no assignee, newcomer labels first,
then recent update order. A platform constraint flags an assignee, a locked conversation,
a native GitHub dependency, or an open PR using a closing keyword. ‘Clear’ means only that
none of those sampled signals fired; it does not mean a person judged the issue suitable.

This is not an independent oracle. Assignee and closing-PR signals overlap production
behavior, and closing keywords deliberately use the exact production parser. They test
integration consistency. Only lock and native-dependency fields can expose a constraint the
current product does not model. Their observed counts are shown below.

All P@10 values use 10 as the fixed denominator. Missing result slots count as
misses, so returning one perfect candidate cannot score the same as returning ten.

## getzep/graphiti

Snapshot: `2026-08-22T11:02:14Z`; fingerprint `b74ccfc9668c4223be15d4d3543f4712835e04880657e4a58306b0519691fd9f`.
GitHub operations counted at the HTTP boundary: 3 GET, **0 writes**.
Platform-only exposure: 0 locked issue(s), 0 native-dependency issue(s).

| ranking | candidates | returned / 10 | clear P@10 | inspections for 3 clear |
|---|---:|---:|---:|---:|
| product available | 20 | 10 | 100.0% | 3 |
| GitHub recent | 50 | 10 | 40.0% | 9 |
| GitHub curated | 47 | 10 | 40.0% | 9 |

Engineering contradiction threshold: 5.0%; result **PASS**.
This is a consistency check, not an estimate of recommendation accuracy.

Assignee-fact ablation on this exact snapshot:

| treatment | available candidates | constraint contradictions | clear P@10 | inspections for 3 clear |
|---|---:|---:|---:|---:|
| assignee facts suppressed | 23 | 3 (13.0%) | 80.0% | 3 |
| current graph | 20 | 0 (0.0%) | 100.0% | 3 |

Constraint-clear items withheld by conservative graph signals:

- [#1645 MCP server: search behavior is hardcoded — proposal: configurable reranker, stale-fact filtering, result counts, relevance scores](https://github.com/getzep/graphiti/issues/1645): open issue (+1.00); already picked up by PR #1790 (open) (-2.00).
- [#1714 RFC: Optional OpenTelemetry metrics + span instrumentation for core memory operations](https://github.com/getzep/graphiti/issues/1714): open issue (+1.00); already picked up by PR #1718 (open) (-2.00).
- [#1723 Anthropic path never emits cache_control, and MODEL_EFFORT does not exist — the "cheap Opus graph extraction" recipe silently bills at full input rate](https://github.com/getzep/graphiti/issues/1723): open issue (+1.00); already picked up by PR #1753 (open) (-2.00).
- [#1727 CLA signatures in the bot's own requested format are silently discarded, blocking 18 open PRs](https://github.com/getzep/graphiti/issues/1727): open issue (+1.00); already picked up by PR #1718 (open), PR #1726 (open), PR #1730 (draft) (-2.00).
- [#1751 OpenAI models rejected with 400 `invalid_json_schema` on OpenRouter](https://github.com/getzep/graphiti/issues/1751): open issue (+1.00); already picked up by PR #1752 (open) (-2.00).

These are not automatically product errors: absence of a sampled platform
constraint is not human validation. They are optional future-review candidates.

## pydantic/pydantic-ai

Snapshot: `2026-08-22T11:02:20Z`; fingerprint `862b38381c9ad47b0e05929a7ef4431dac748fc7731eabfb9aaa90f06c75bf77`.
GitHub operations counted at the HTTP boundary: 4 GET, **0 writes**.
Platform-only exposure: 0 locked issue(s), 0 native-dependency issue(s).

| ranking | candidates | returned / 10 | clear P@10 | inspections for 3 clear |
|---|---:|---:|---:|---:|
| product available | 7 | 7 | 70.0% | 3 |
| GitHub recent | 50 | 10 | 30.0% | 3 |
| GitHub curated | 15 | 10 | 70.0% | 3 |

Engineering contradiction threshold: 5.0%; result **PASS**.
This is a consistency check, not an estimate of recommendation accuracy.

Assignee-fact ablation on this exact snapshot:

| treatment | available candidates | constraint contradictions | clear P@10 | inspections for 3 clear |
|---|---:|---:|---:|---:|
| assignee facts suppressed | 41 | 34 (82.9%) | 0.0% | 37 |
| current graph | 7 | 0 (0.0%) | 70.0% | 3 |

Constraint-clear items withheld by conservative graph signals:

- [#7648 Add multimodal `ToolReturn.content` durability coverage for DBOS and Prefect](https://github.com/pydantic/pydantic-ai/issues/7648): open issue (+1.00); already picked up by PR #7618 (open) (-2.00).
- [#7683 Test files importing `snapshot` from `inline_snapshot` instead of the `tests._inline_snapshot` wrapper fail locally with an internal `UsageError`](https://github.com/pydantic/pydantic-ai/issues/7683): open issue (+1.00); already picked up by PR #7681 (open) (-2.00).

These are not automatically product errors: absence of a sampled platform
constraint is not human validation. They are optional future-review candidates.

## trustgraph-ai/trustgraph

Snapshot: `2026-08-22T11:02:22Z`; fingerprint `c20e9f8383ca3cc673290f98fa13faa55224ced245b352e659dd1ebe6d7c8899`.
GitHub operations counted at the HTTP boundary: 2 GET, **0 writes**.
Platform-only exposure: 0 locked issue(s), 0 native-dependency issue(s).

| ranking | candidates | returned / 10 | clear P@10 | inspections for 3 clear |
|---|---:|---:|---:|---:|
| product available | 14 | 10 | 100.0% | 3 |
| GitHub recent | 17 | 10 | 90.0% | 3 |
| GitHub curated | 15 | 10 | 100.0% | 3 |

Engineering contradiction threshold: 5.0%; result **PASS**.
This is a consistency check, not an estimate of recommendation accuracy.

Assignee-fact ablation on this exact snapshot:

| treatment | available candidates | constraint contradictions | clear P@10 | inspections for 3 clear |
|---|---:|---:|---:|---:|
| assignee facts suppressed | 16 | 2 (12.5%) | 80.0% | 3 |
| current graph | 14 | 0 (0.0%) | 100.0% | 3 |

Constraint-clear items withheld by conservative graph signals:

- [#783 Replace bare except: with specific exceptions](https://github.com/trustgraph-ai/trustgraph/issues/783): open issue (+1.00); labeled good first issue (+0.75); already picked up by PR #1097 (open) (-2.00).

These are not automatically product errors: absence of a sampled platform
constraint is not human validation. They are optional future-review candidates.

## What remains unproven

- Time-to-selection needs a timed A/B task with contributors; the inspection count is only a proxy.
  Pilot 0 does not require recruiting participants because it makes no human-outcome claim.
- Maintainer burden needs maintainer feedback; this run proves only that its measured write count is zero.
- The snapshot samples recent open items and the latest repository-wide comments, so it can miss
  old comments or PRs outside the sample. A GitHub App backfill is not part of Pilot 0.
- A plain PR reference is ambiguous. Closing keywords count as a platform constraint; ambiguous
  references are listed for manual review instead of being declared right or wrong.
- Because the constraint evaluator shares the production closing parser, it cannot measure that
  parser's accuracy. Parser behavior is covered by tests, not by these live headline metrics.
- Pilot 0 uses the deterministic GitHub layer only. Semantic fit and personalization require a
  separate evaluation after factual availability is reliable.
