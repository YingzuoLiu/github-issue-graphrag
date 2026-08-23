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

Unit and replay tests prove implementation invariants. They do not measure product usefulness.
`scripts/run_real_repo_pilot.py` samples current public GitHub data and produces a timestamped,
fingerprinted result with engineering consistency and coverage measures, so a poor result cannot
be hidden behind a successful demo.

The runner has only GET operations. It cannot comment, label, assign, close, install a webhook or
otherwise mutate a pilot repository. Its HTTP session counts non-GET requests, so the zero-write
check will fail if a future change introduces one; zero is not a report constant.

## What stays deterministic

Pilot 0 uses no LLM. These are deterministic:

- repository selection and sample limits;
- GitHub API collection and request counts;
- assignee, lock and native dependency signals;
- open pull requests with GitHub closing keywords;
- current graph projection and contribution score;
- baselines, ranking metrics and report generation;
- the snapshot fingerprint and the measured assertion that GitHub writes equal zero.

Model extraction is deferred. Semantic relevance cannot be judged safely until factual
availability is reliable, and an LLM cannot serve as the ground truth for its own recommendation.

## M1 regression evidence baseline

The live pilot and the deterministic release gate have different jobs:

- `tests/fixtures/contribution/graphiti_snapshot.json` is a compact, checked-in snapshot of 25
  public Graphiti issues, 25 public pull requests and 50 recent public comments. It records the
  GitHub API source, fetch time, collection limits, two measured GET requests, zero writes and a
  content fingerprint.
- `tests/fixtures/contribution/graphiti_expected.json` freezes the exact production
  `opportunities()` ordering plus every issue's `status`, `score`, `reasons` and evidence links.
- `test_graphiti_contribution_contract_matches_reviewed_golden_snapshot` replays the snapshot
  offline in CI. A scoring, classification, explanation, evidence or ordering change requires an
  explicit expected-file update and review.

The fixture is not refreshed automatically. The refresh command defaults to a no-write preview:

```bash
python scripts/refresh_contribution_fixture.py
```

It prints the candidate fingerprint, HTTP counts and status distribution. After reviewing that
preview, an intentional contract update is accepted with:

```bash
python scripts/refresh_contribution_fixture.py --accept
```

`--accept` replaces both files together; the resulting diff is the review surface. This checked-in
snapshot is deterministic CI evidence, not a claim that current Graphiti data still looks the same.

## Repositories

The initial set is intentionally not three copies of the same workload:

| Repository | Role in the pilot |
|---|---|
| `getzep/graphiti` | high issue/PR activity in a graph and agent-adjacent codebase |
| `pydantic/pydantic-ai` | high activity with a different product and maintainer workflow |
| `trustgraph-ai/trustgraph` | smaller active repository with newcomer labels; a control where native GitHub may already be sufficient |

`microsoft/graphrag` was considered but had too little recent open activity at selection time.
`mem0ai/mem0` remains a replacement if one selected repository becomes unavailable.

## Pilot 0: read-only engineering consistency check

The runner samples recent open issues, open pull requests and the latest repository-wide comments.
It compares three rankings:

1. **Product available:** issues the current graph calls `available`.
2. **GitHub recent:** open issues in GitHub's updated order.
3. **GitHub curated:** unassigned issues, newcomer labels first, then GitHub's updated order.

The platform-constraint label flags an issue if it has an assignee, is locked, has a native blocking
dependency, or an open PR uses a closing keyword for it. A plain PR reference is ambiguous; the
report lists it for possible future review instead of declaring it correct or incorrect.

This label is **not an independent oracle**. Assignee and closing-PR signals overlap production
behavior, and closing keywords intentionally use the exact production parser so the evaluator
cannot drift from it. Those checks measure integration consistency, not parser or recommendation
accuracy. Locked and native-dependency fields are the only sampled constraints the product does
not currently model, so the report exposes their occurrence counts instead of treating a zero
contradiction rate as general quality evidence.

Engineering checks and measures:

- the rate of product-available items that contradict a sampled platform constraint must be at
  most the originally precommitted 5% threshold;
- every non-available recommendation must link causal evidence;
- measured GitHub write requests must equal zero;
- constraint-clear precision at 10 is compared with both native baselines, always dividing by 10;
  an unfilled result slot is a miss;
- coverage records how many constraint-clear issues the product withholds as a conservative
  `claimed` result;
- the report records how many entries must be inspected to find three constraint-clear choices.

Each run also suppresses assignee facts in an ablation over the exact same in-memory snapshot.
That comparison isolates the effect of assignee coverage from repository activity between runs.

If any repository exceeds the contradiction threshold, the integration is fixed before making a
human-facing claim. The threshold is not relaxed after seeing the result.

## Observed result (2026-08-22)

The first run exceeded the originally named 5% “false available” gate in all three repositories.
Every contradiction was assigned on GitHub while the live schema did not yet model assignees.
After adding versioned `assignees`, `CONTRIBUTOR` nodes and GitHub-only `assigned_to` facts, the
same consistency check passed in all three repositories. The ablation below suppresses only
assignee facts on the exact same post-fix snapshot, so it isolates that integration repair from
repository activity.

| Repository | Contradictions without assignee facts | Current contradictions | Product clear P@10 | Constraint-clear coverage | Inspections for 3 (product / recent / curated) |
|---|---:|---:|---:|---:|---:|
| `getzep/graphiti` | 13.0% | 0.0% | 100.0% | 80.0% | 3 / 9 / 9 |
| `pydantic/pydantic-ai` | 82.9% | 0.0% | 70.0% | 77.8% | 3 / 3 / 3 |
| `trustgraph-ai/trustgraph` | 12.5% | 0.0% | 100.0% | 93.3% | 3 / 3 / 3 |

The zero current contradictions confirm that the repaired product reads the sampled shared facts
consistently; they do not estimate recommendation accuracy. In this sample, locked and native
dependency counts were zero in every repository, so the only two non-overlapping constraint paths
received no live exposure. The fixed-denominator correction also changes PydanticAI from the old
6/6 = 100% presentation to 6/10 = 60% on the reviewed snapshot; the refreshed committed run has
seven returned candidates and correctly reports 7/10 = 70%.

The graph conservatively withheld five, two and one constraint-clear issues respectively because
an open PR merely referenced them. A reference may or may not mean the work is claimed, so those
cases remain optional future-review candidates. The committed
[post-fix report](../eval/pilot_results_after_assignee.md) contains the evidence links and exact
snapshot fingerprints; the [initial failure](../eval/pilot_results_before_assignee.md) is retained
as an audit trail under the original terminology.

The most useful result is narrower: assignee modeling prevents a demonstrated integration error.
Graphiti's closing-keyword PR links coincide with an inspection-depth advantage over GitHub's
curated baseline. PydanticAI and TrustGraph tie that baseline in the refreshed run, so the current
data does not support a general claim that the graph reduces inspection burden. TrustGraph, where
no closing-keyword link fired, remains a useful boundary where native GitHub appears sufficient.

## What Pilot 0 cannot prove

Top-k precision and inspection depth are proxies. They do not prove that a person selects work
faster. Zero writes prove that the run did not touch a repository, not that maintainers perceive no
burden.

Those questions would require two later stages if the project later makes human-outcome claims.
They are not required to merge the engineering work or to interpret Pilot 0.

### Pilot 1: contributor task

- If recruitment becomes practical, target at least five contributors who are not maintainers of
  the selected repository.
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

An optional `GITHUB_TOKEN` raises the read-rate limit. Each run reserves an immutable UTC directory,
for example `eval/pilot_runs/20260823T020000Z/`, containing `results.json` and `report.md`. Reusing a
run directory or explicit output path fails instead of overwriting history. The report stores URLs,
compact examples and a fingerprint, not a repository-wide copy of issue and comment bodies.

`.github/workflows/contribution-pilot.yml` runs the same read-only command every Monday and on
manual dispatch. It has `contents: read`, uploads the timestamped directory as a 30-day workflow
artifact and fails if the contradiction, causal-evidence or zero-write engineering gate fails.
These time-varying runs are monitoring evidence; they do not replace the checked-in golden fixture
and do not run as deterministic pull-request CI.
