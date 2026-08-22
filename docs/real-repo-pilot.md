# Real-repository contribution pilot

## Problem

An active repository can have hundreds of open issues, pull requests and comments. A contributor
needs to decide which issue is genuinely available before writing code. An open issue alone is not
enough: it may already be assigned, blocked, covered by a pull request or stale in ways that are
only visible after opening several pages.

The product claim to test is therefore narrow:

> A live, evidence-backed repository graph helps a contributor find a genuinely actionable issue
> with fewer inspections, without adding work to the repository or its maintainers.

## Existing solution

GitHub already provides issue search and filters, labels, assignees, Projects, explicit issue
dependencies and pull-request links. A strong native baseline can filter to unassigned issues,
prioritize `good first issue` / `help wanted`, and then sort by recent activity. The pilot must beat
that baseline or identify a job GitHub does not already perform; merely drawing the same fields as a
graph is not a product gap.

- [Filtering and searching issues and pull requests](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/filtering-and-searching-issues-and-pull-requests)
- [Creating issue dependencies](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies)
- [Linking a pull request to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)

## Gap to test

GitHub exposes the facts, but a contributor may still need to combine several views and read prose
to understand availability. The live graph claims to perform that combination, keep it current and
show the evidence behind the result. It does **not** claim to replace GitHub Projects, issue search
or a coding agent.

The first evaluation must answer whether the current implementation combines those facts
correctly. Only after that is it meaningful to test semantic fit or personalized recommendations.

## Why a dedicated pilot runner is needed

Unit and replay tests prove implementation invariants. They do not measure repository coverage,
ranking quality or product usefulness. `scripts/run_real_repo_pilot.py` samples current public
GitHub data and produces a timestamped, fingerprinted result with precommitted metrics, so a poor
result cannot be hidden behind a successful demo.

The runner has only GET operations. It cannot comment, label, assign, close, install a webhook or
otherwise mutate a pilot repository.

## What stays deterministic

Pilot 0 uses no LLM. These are deterministic:

- repository selection and sample limits;
- GitHub API collection and request counts;
- assignee, lock and native dependency signals;
- open pull requests with GitHub closing keywords;
- current graph projection and contribution score;
- baselines, ranking metrics and report generation;
- the snapshot fingerprint and the assertion that GitHub writes equal zero.

Model extraction is deferred. Semantic relevance cannot be judged safely until factual
availability is reliable, and an LLM cannot serve as the ground truth for its own recommendation.

## Repositories

The initial set is intentionally not three copies of the same workload:

| Repository | Role in the pilot |
|---|---|
| `getzep/graphiti` | high issue/PR activity in a graph and agent-adjacent codebase |
| `pydantic/pydantic-ai` | high activity with a different product and maintainer workflow |
| `trustgraph-ai/trustgraph` | smaller active repository with newcomer labels; a control where native GitHub may already be sufficient |

`microsoft/graphrag` was considered but had too little recent open activity at selection time.
`mem0ai/mem0` remains a replacement if one selected repository becomes unavailable.

## Pilot 0: read-only engineering gate

The runner samples recent open issues, open pull requests and the latest repository-wide comments.
It compares three rankings:

1. **Product available:** issues the current graph calls `available`.
2. **GitHub recent:** open issues in GitHub's updated order.
3. **GitHub curated:** unassigned issues, newcomer labels first, then GitHub's updated order.

The actionability oracle uses only strong platform evidence. An issue is not actionable if it has
an assignee, is locked, has a native blocking dependency, or an open PR uses a closing keyword for
it. A plain PR reference is ambiguous; the report lists it for manual review instead of declaring
it correct or incorrect.

Precommitted checks:

- false-available rate must be at most 5%;
- every non-available recommendation must link causal evidence;
- GitHub write requests must equal zero;
- product precision at 10 is compared with both native baselines;
- coverage records how many oracle-actionable issues the product withholds as a conservative
  `claimed` result;
- the report records how many entries must be inspected to find three actionable choices.

Each run also suppresses assignee facts in an ablation over the exact same in-memory snapshot.
That comparison isolates the effect of assignee coverage from repository activity between runs.

If any repository exceeds the false-available threshold, factual coverage is fixed before a user
study. The threshold is not relaxed after seeing the result.

## Observed result (2026-08-22)

The first run failed the precommitted 5% false-available gate in all three repositories. Every
false-available example was assigned on GitHub, while the live schema did not yet model assignees.
After adding versioned `assignees`, `CONTRIBUTOR` nodes and GitHub-only `assigned_to` facts, the
same evaluation passed in all three repositories. The ablation below suppresses only assignee
facts on the exact same post-fix snapshot, so it isolates that change from repository activity.

| Repository | False available without assignee facts | Current false available | Product P@10 | Oracle-actionable coverage | Inspections for 3 (product / recent / curated) |
|---|---:|---:|---:|---:|---:|
| `getzep/graphiti` | 13.0% | 0.0% | 100.0% | 80.0% | 3 / 9 / 9 |
| `pydantic/pydantic-ai` | 85.0% | 0.0% | 100.0% | 75.0% | 3 / 37 / 6 |
| `trustgraph-ai/trustgraph` | 12.5% | 0.0% | 100.0% | 93.3% | 3 / 3 / 3 |

This clears the factual safety gate but does not settle product usefulness. The graph conservatively
withheld five, two and one oracle-actionable issues respectively because an open PR merely
referenced them. A reference may or may not mean the work is claimed, so those cases are carried
forward as a human-review set instead of being relabeled after seeing the result. The committed
[post-fix report](../eval/pilot_results_after_assignee.md) contains the evidence links and exact
snapshot fingerprints; the [initial failure](../eval/pilot_results_before_assignee.md) is retained
as an audit trail.

## What Pilot 0 cannot prove

Top-k precision and inspection depth are proxies. They do not prove that a person selects work
faster. Zero writes prove that the run did not touch a repository, not that maintainers perceive no
burden.

Those questions require two later stages:

### Pilot 1: contributor task

- Recruit at least five contributors who are not maintainers of the selected repository.
- Give each person two counterbalanced tasks: choose an issue with GitHub alone and with the pilot
  view.
- Measure time to a defensible choice, pages opened, abandoned choices and confidence.
- Have a maintainer or repository expert review the chosen issue without seeing which interface
  produced it.

### Pilot 2: maintainer check

- Share a private read-only report; do not post automated comments on issues.
- Ask whether the unavailable reasons are correct, whether any recommendation creates cleanup
  work, and whether a digest would be useful.
- Add write permissions or notifications only through a separate opt-in design after this check.

## Run

```bash
python scripts/run_real_repo_pilot.py
```

An optional `GITHUB_TOKEN` raises the read-rate limit. The outputs are
`eval/pilot_results.json` and `eval/pilot_results.md`. The report stores URLs, compact examples and
a fingerprint, not a repository-wide copy of issue and comment bodies.
