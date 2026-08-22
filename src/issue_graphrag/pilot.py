"""Read-only evaluation of contribution rankings against live GitHub facts.

Pilot 0 is deliberately narrower than a user study. It can test whether the
ranking contradicts explicit GitHub facts and compare inspection-burden
proxies. It cannot prove that a person works faster or that maintainers like
the product; those require observed tasks and interviews.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from issue_graphrag.ingest.github_loader import parse_repo, to_seed_item
from issue_graphrag.live.contribution import HELP_LABELS, opportunities
from issue_graphrag.live.indexer import NullExtractor, bootstrap
from issue_graphrag.live.models import Opportunity
from issue_graphrag.live.projection import project_graph
from issue_graphrag.live.records import seed_items
from issue_graphrag.live.timeutil import now_utc, to_iso

DEFAULT_PILOT_REPOS = (
    "getzep/graphiti",
    "pydantic/pydantic-ai",
    "trustgraph-ai/trustgraph",
)
DEFAULT_FALSE_AVAILABLE_THRESHOLD = 0.05

_TARGET = r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)\b"
_REFERENCE = re.compile(rf"(?<![\w#]){_TARGET}")
_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*"
    + _TARGET,
    re.IGNORECASE,
)
_ISSUE_URL_NUMBER = re.compile(r"/issues/(\d+)$")


@dataclass(frozen=True)
class PilotSnapshot:
    repo: str
    fetched_at: str
    raw_items: list[dict[str, Any]]
    recent_comments: list[dict[str, Any]]
    seed: dict[str, Any]
    request_count: int
    fingerprint: str

    @property
    def issues(self) -> list[dict[str, Any]]:
        return [item for item in self.raw_items if "pull_request" not in item]

    @property
    def pulls(self) -> list[dict[str, Any]]:
        return [item for item in self.raw_items if "pull_request" in item]


class GitHubPilotClient:
    """A GET-only GitHub client with an explicit request budget."""

    def __init__(
        self,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.token = token
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.request_count = 0

    def _get_list(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-issue-graphrag-pilot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.request_count += 1
        response = self.session.get(
            url,
            headers=headers,
            params=params,
            timeout=self.timeout_seconds,
        )
        if response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0":
            raise RuntimeError("GitHub API rate limit exhausted; configure GITHUB_TOKEN")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError(f"GitHub returned a non-list payload for {url}")
        return [item for item in payload if isinstance(item, dict)]

    def fetch_snapshot(
        self,
        repo: str,
        issue_limit: int = 50,
        pull_limit: int = 50,
        max_pages: int = 3,
        comment_limit: int = 100,
        fetched_at: str | None = None,
    ) -> PilotSnapshot:
        """Fetch recent open issues/PRs plus one repository-wide comment page."""
        owner, name = parse_repo(repo)
        if issue_limit < 1 or pull_limit < 1:
            raise ValueError("issue_limit and pull_limit must be positive")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        if not 0 <= comment_limit <= 100:
            raise ValueError("comment_limit must be between 0 and 100")

        start_requests = self.request_count
        selected: list[dict[str, Any]] = []
        issue_count = 0
        pull_count = 0
        seen: set[int] = set()
        issues_url = f"https://api.github.com/repos/{owner}/{name}/issues"

        for page in range(1, max_pages + 1):
            batch = self._get_list(
                issues_url,
                {
                    "state": "open",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not batch:
                break
            for raw in batch:
                number = int(raw["number"])
                if number in seen:
                    continue
                is_pull = "pull_request" in raw
                if is_pull and pull_count >= pull_limit:
                    continue
                if not is_pull and issue_count >= issue_limit:
                    continue
                selected.append(raw)
                seen.add(number)
                if is_pull:
                    pull_count += 1
                else:
                    issue_count += 1
            if issue_count >= issue_limit and pull_count >= pull_limit:
                break
            if len(batch) < 100:
                break

        comments: list[dict[str, Any]] = []
        if comment_limit:
            comments = self._get_list(
                f"https://api.github.com/repos/{owner}/{name}/issues/comments",
                {
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": comment_limit,
                },
            )

        return make_snapshot(
            repo,
            selected,
            comments,
            fetched_at=fetched_at or to_iso(now_utc()),
            request_count=self.request_count - start_requests,
        )


def _comment_number(comment: dict[str, Any]) -> int | None:
    match = _ISSUE_URL_NUMBER.search(str(comment.get("issue_url") or ""))
    return int(match.group(1)) if match else None


def _fingerprint_payload(
    seed: dict[str, Any],
    raw_items: list[dict[str, Any]],
) -> str:
    explicit_signals = [
        {
            "number": item.get("number"),
            "assignees": sorted(
                str(assignee.get("login") or "")
                for assignee in item.get("assignees") or []
                if isinstance(assignee, dict)
            ),
            "locked": bool(item.get("locked")),
            "issue_dependencies_summary": item.get("issue_dependencies_summary") or {},
        }
        for item in raw_items
        if "pull_request" not in item
    ]
    payload = {"seed": seed, "explicit_signals": explicit_signals}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_snapshot(
    repo: str,
    raw_items: list[dict[str, Any]],
    recent_comments: list[dict[str, Any]],
    fetched_at: str,
    request_count: int,
) -> PilotSnapshot:
    """Build the same seed used by the live index while retaining oracle signals."""
    parse_repo(repo)
    comments_by_number: dict[int, list[dict[str, Any]]] = {}
    for comment in recent_comments:
        number = _comment_number(comment)
        if number is not None:
            comments_by_number.setdefault(number, []).append(comment)

    records = []
    for raw in raw_items:
        number = int(raw["number"])
        kind = "pull_request" if "pull_request" in raw else "issue"
        records.append(
            to_seed_item(
                repo,
                raw,
                kind,
                comments=comments_by_number.get(number, []),
                files=[],
            )
        )
    seed = {"repo": repo, "items": records}
    return PilotSnapshot(
        repo=repo,
        fetched_at=to_iso(fetched_at),
        raw_items=raw_items,
        recent_comments=recent_comments,
        seed=seed,
        request_count=request_count,
        fingerprint=_fingerprint_payload(seed, raw_items),
    )


def _labels(raw: dict[str, Any]) -> list[str]:
    return sorted(
        str(label.get("name") or "")
        for label in raw.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    )


def _assignees(raw: dict[str, Any]) -> list[str]:
    return sorted(
        str(assignee.get("login") or "")
        for assignee in raw.get("assignees") or []
        if isinstance(assignee, dict) and assignee.get("login")
    )


def _blocked_by_count(raw: dict[str, Any]) -> int:
    summary = raw.get("issue_dependencies_summary") or {}
    if not isinstance(summary, dict):
        return 0
    return int(summary.get("blocked_by") or summary.get("total_blocked_by") or 0)


def _local_number(repo: str, match: re.Match[str]) -> int | None:
    qualifier = match.group("repo")
    if qualifier and qualifier.casefold() != repo.casefold():
        return None
    return int(match.group("number"))


def _pr_links(snapshot: PilotSnapshot) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    issue_numbers = {int(raw["number"]) for raw in snapshot.issues}
    strong: dict[int, list[int]] = {}
    weak: dict[int, list[int]] = {}
    for pull in snapshot.pulls:
        pull_number = int(pull["number"])
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        strong_numbers = {
            number
            for match in _CLOSING.finditer(text)
            if (number := _local_number(snapshot.repo, match)) is not None
        }
        reference_numbers = {
            number
            for match in _REFERENCE.finditer(text)
            if (number := _local_number(snapshot.repo, match)) is not None
        }
        for number in sorted(strong_numbers & issue_numbers):
            strong.setdefault(number, []).append(pull_number)
        for number in sorted((reference_numbers - strong_numbers) & issue_numbers):
            weak.setdefault(number, []).append(pull_number)
    return strong, weak


def _oracle_reasons(raw: dict[str, Any], strong_claims: list[int]) -> list[str]:
    reasons: list[str] = []
    assignees = _assignees(raw)
    if assignees:
        reasons.append(f"assigned to {', '.join(assignees)}")
    if raw.get("locked"):
        reasons.append("issue conversation is locked")
    blocked_by = _blocked_by_count(raw)
    if blocked_by:
        reasons.append(f"GitHub reports {blocked_by} blocking dependencies")
    if strong_claims:
        reasons.append("open PR with closing keyword: " + ", ".join(f"#{n}" for n in strong_claims))
    return reasons


def _ranking_metrics(
    numbers: list[int],
    actionable: set[int],
    top_k: int,
    target_choices: int = 3,
) -> dict[str, Any]:
    top = numbers[:top_k]
    precision = sum(number in actionable for number in top) / len(top) if top else None
    found = 0
    inspections: int | None = None
    for rank, number in enumerate(numbers, start=1):
        if number in actionable:
            found += 1
            if found == target_choices:
                inspections = rank
                break
    return {
        "candidate_count": len(numbers),
        "precision_at_k": round(precision, 4) if precision is not None else None,
        "inspections_for_three_actionable": inspections,
        "top_numbers": top,
    }


def _issue_summary(
    raw: dict[str, Any],
    status: str | None,
    score: float | None,
    oracle_reasons: list[str],
) -> dict[str, Any]:
    return {
        "number": int(raw["number"]),
        "title": str(raw.get("title") or ""),
        "url": raw.get("html_url"),
        "labels": _labels(raw),
        "assignees": _assignees(raw),
        "system_status": status,
        "system_score": score,
        "oracle_actionable": not oracle_reasons,
        "oracle_reasons": oracle_reasons,
    }


def _rank_snapshot(
    snapshot: PilotSnapshot, *, include_assignees: bool
) -> list[Opportunity]:
    items = seed_items(snapshot.repo, snapshot.seed["items"])
    if not include_assignees:
        for item in items.values():
            item.assignees = []
    state = bootstrap(snapshot.repo, items, NullExtractor())
    return opportunities(project_graph(state))


def evaluate_snapshot(
    snapshot: PilotSnapshot,
    top_k: int = 10,
    false_available_threshold: float = DEFAULT_FALSE_AVAILABLE_THRESHOLD,
) -> dict[str, Any]:
    """Compare current ranking output with independent, explicit GitHub signals."""
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if not 0 <= false_available_threshold <= 1:
        raise ValueError("false_available_threshold must be between 0 and 1")

    ranked = _rank_snapshot(snapshot, include_assignees=True)
    without_assignee_facts = _rank_snapshot(snapshot, include_assignees=False)
    by_number = {item.number: item for item in ranked}
    raw_by_number = {int(raw["number"]): raw for raw in snapshot.issues}
    strong_claims, weak_claims = _pr_links(snapshot)

    reasons_by_number = {
        number: _oracle_reasons(raw, strong_claims.get(number, []))
        for number, raw in raw_by_number.items()
    }
    actionable = {
        number for number, reasons in reasons_by_number.items() if not reasons
    }

    product_numbers = [item.number for item in ranked if item.status == "available"]
    ablation_numbers = [
        item.number for item in without_assignee_facts if item.status == "available"
    ]
    recent_numbers = [int(raw["number"]) for raw in snapshot.issues]
    unassigned = [raw for raw in snapshot.issues if not _assignees(raw) and not raw.get("locked")]
    help_labeled = [
        raw
        for raw in unassigned
        if any(label.casefold() in HELP_LABELS for label in _labels(raw))
    ]
    help_numbers = {int(raw["number"]) for raw in help_labeled}
    curated_numbers = [int(raw["number"]) for raw in help_labeled] + [
        int(raw["number"]) for raw in unassigned if int(raw["number"]) not in help_numbers
    ]

    false_available_numbers = [
        number for number in product_numbers if reasons_by_number.get(number)
    ]
    false_available_rate = (
        len(false_available_numbers) / len(product_numbers) if product_numbers else None
    )
    ablation_false_numbers = [
        number for number in ablation_numbers if reasons_by_number.get(number)
    ]
    ablation_false_rate = (
        len(ablation_false_numbers) / len(ablation_numbers) if ablation_numbers else None
    )

    product_set = set(product_numbers)
    returned_actionable = product_set & actionable
    actionable_coverage = (
        len(returned_actionable) / len(actionable) if actionable else None
    )
    unreturned_actionable = [
        item.number
        for item in ranked
        if item.number in actionable and item.number not in product_set
    ]

    causal_evidence = [
        any(evidence.url for evidence in item.evidence[1:])
        for item in ranked
        if item.status != "available"
    ]
    evidence_coverage = (
        sum(causal_evidence) / len(causal_evidence) if causal_evidence else 1.0
    )

    ambiguous_claims = [
        number
        for number, item in by_number.items()
        if item.status == "claimed"
        and not reasons_by_number.get(number)
        and weak_claims.get(number)
    ]

    product_metrics = _ranking_metrics(product_numbers, actionable, top_k)
    ablation_metrics = _ranking_metrics(ablation_numbers, actionable, top_k)
    ablation_metrics.update(
        {
            "false_available_count": len(ablation_false_numbers),
            "false_available_rate": (
                round(ablation_false_rate, 4)
                if ablation_false_rate is not None
                else None
            ),
        }
    )
    recent_metrics = _ranking_metrics(recent_numbers, actionable, top_k)
    curated_metrics = _ranking_metrics(curated_numbers, actionable, top_k)
    false_rate_pass = (
        false_available_rate is not None
        and false_available_rate <= false_available_threshold
    )

    false_examples = [
        _issue_summary(
            raw_by_number[number],
            by_number[number].status if number in by_number else None,
            by_number[number].score if number in by_number else None,
            reasons_by_number[number],
        )
        for number in false_available_numbers[:10]
    ]
    top_product = [
        _issue_summary(
            raw_by_number[number],
            by_number[number].status,
            by_number[number].score,
            reasons_by_number[number],
        )
        for number in product_numbers[:top_k]
        if number in raw_by_number and number in by_number
    ]
    ambiguous_examples = [
        {
            **_issue_summary(
                raw_by_number[number],
                by_number[number].status,
                by_number[number].score,
                reasons_by_number[number],
            ),
            "referenced_by_open_prs": weak_claims.get(number, []),
            "review_note": "plain PR reference is not independent proof that work is claimed",
        }
        for number in ambiguous_claims[:10]
        if number in raw_by_number
    ]
    unreturned_examples = [
        {
            **_issue_summary(
                raw_by_number[number],
                by_number[number].status,
                by_number[number].score,
                reasons_by_number[number],
            ),
            "system_reasons": by_number[number].reasons,
            "system_evidence": [
                evidence.model_dump() for evidence in by_number[number].evidence[1:]
            ],
            "known_open_pr_plain_references": weak_claims.get(number, []),
            "review_note": (
                "explicit GitHub oracle found no unavailable signal; conservative system "
                "evidence needs human review"
            ),
        }
        for number in unreturned_actionable[:10]
        if number in raw_by_number and number in by_number
    ]

    return {
        "repo": snapshot.repo,
        "fetched_at": snapshot.fetched_at,
        "snapshot_fingerprint": snapshot.fingerprint,
        "collection": {
            "open_issues": len(snapshot.issues),
            "open_pull_requests": len(snapshot.pulls),
            "recent_comments": len(snapshot.recent_comments),
            "github_read_requests": snapshot.request_count,
            "github_write_requests": 0,
            "reported_comment_count_on_sampled_items": sum(
                int(raw.get("comments") or 0) for raw in snapshot.raw_items
            ),
        },
        "system_status_counts": {
            status: sum(item.status == status for item in ranked)
            for status in ("available", "claimed", "blocked")
        },
        "oracle": {
            "actionable": len(actionable),
            "explicitly_unavailable": len(raw_by_number) - len(actionable),
            "assigned": sum(bool(_assignees(raw)) for raw in snapshot.issues),
            "locked": sum(bool(raw.get("locked")) for raw in snapshot.issues),
            "blocked_by_dependency": sum(_blocked_by_count(raw) > 0 for raw in snapshot.issues),
            "claimed_by_closing_pr": len(strong_claims),
        },
        "metrics": {
            "product_available_ranking": product_metrics,
            "without_assignee_fact_ablation": ablation_metrics,
            "github_recent_open_baseline": recent_metrics,
            "github_unassigned_curated_baseline": curated_metrics,
            "false_available_count": len(false_available_numbers),
            "false_available_rate": (
                round(false_available_rate, 4) if false_available_rate is not None else None
            ),
            "causal_evidence_url_coverage": round(evidence_coverage, 4),
            "ambiguous_plain_reference_claims": len(ambiguous_claims),
            "oracle_actionable_coverage": (
                round(actionable_coverage, 4) if actionable_coverage is not None else None
            ),
            "oracle_actionable_not_returned_count": len(unreturned_actionable),
        },
        "precommitted_checks": {
            "false_available_rate_at_most": false_available_threshold,
            "false_available_rate_pass": false_rate_pass,
            "all_non_available_results_have_causal_evidence_url": evidence_coverage == 1.0,
            "github_write_requests_are_zero": True,
            "beats_recent_baseline_at_top_k": (
                product_metrics["precision_at_k"] is not None
                and recent_metrics["precision_at_k"] is not None
                and product_metrics["precision_at_k"] > recent_metrics["precision_at_k"]
            ),
        },
        "top_product_candidates": top_product,
        "false_available_examples": false_examples,
        "ambiguous_claim_examples": ambiguous_examples,
        "oracle_actionable_not_returned_examples": unreturned_examples,
    }


def render_markdown(results: list[dict[str, Any]], top_k: int) -> str:
    """Render a compact, honest report suitable for committing with the run."""
    lines = [
        "# Real-repository contribution pilot",
        "",
        "Pilot 0 is a read-only engineering evaluation, not a user study. It tests contradictions",
        "against explicit GitHub facts and two inspection-burden proxies. It does **not** prove that",
        "contributors work faster or that maintainers perceive less burden.",
        "",
        "## Summary",
        "",
        (
            f"| repository | issues | PRs | API GETs | false available | product P@{top_k} | "
            f"actionable coverage | recent P@{top_k} | curated P@{top_k} |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        collection = result["collection"]
        metrics = result["metrics"]
        lines.append(
            f"| [{result['repo']}](https://github.com/{result['repo']}) | "
            f"{collection['open_issues']} | {collection['open_pull_requests']} | "
            f"{collection['github_read_requests']} | "
            f"{metrics['false_available_count']} ({_percent(metrics['false_available_rate'])}) | "
            f"{_percent(metrics['product_available_ranking']['precision_at_k'])} | "
            f"{_percent(metrics['oracle_actionable_coverage'])} | "
            f"{_percent(metrics['github_recent_open_baseline']['precision_at_k'])} | "
            f"{_percent(metrics['github_unassigned_curated_baseline']['precision_at_k'])} |"
        )

    lines.extend(
        [
            "",
            "The curated baseline uses only native GitHub fields: no assignee, newcomer labels first,",
            "then recent update order. The actionability oracle excludes issues with an assignee, a",
            "locked conversation, a GitHub dependency, or an open PR using a closing keyword.",
            "",
        ]
    )

    for result in results:
        metrics = result["metrics"]
        checks = result["precommitted_checks"]
        collection = result["collection"]
        lines.extend(
            [
                f"## {result['repo']}",
                "",
                f"Snapshot: `{result['fetched_at']}`; fingerprint `{result['snapshot_fingerprint']}`.",
                f"GitHub operations: {collection['github_read_requests']} GET, **0 writes**.",
                "",
                "| ranking | candidates | precision | inspections for 3 actionable |",
                "|---|---:|---:|---:|",
            ]
        )
        for label, key in (
            ("product available", "product_available_ranking"),
            ("GitHub recent", "github_recent_open_baseline"),
            ("GitHub curated", "github_unassigned_curated_baseline"),
        ):
            row = metrics[key]
            lines.append(
                f"| {label} | {row['candidate_count']} | {_percent(row['precision_at_k'])} | "
                f"{row['inspections_for_three_actionable'] or 'n/a'} |"
            )
        lines.extend(
            [
                "",
                f"Precommitted false-available threshold: "
                f"{_percent(checks['false_available_rate_at_most'])}; "
                f"result **{'PASS' if checks['false_available_rate_pass'] else 'FAIL'}**.",
                "",
                "Assignee-fact ablation on this exact snapshot:",
                "",
                "| treatment | available candidates | false available | precision | inspections for 3 actionable |",
                "|---|---:|---:|---:|---:|",
                (
                    f"| assignee facts suppressed | "
                    f"{metrics['without_assignee_fact_ablation']['candidate_count']} | "
                    f"{metrics['without_assignee_fact_ablation']['false_available_count']} "
                    f"({_percent(metrics['without_assignee_fact_ablation']['false_available_rate'])}) | "
                    f"{_percent(metrics['without_assignee_fact_ablation']['precision_at_k'])} | "
                    f"{metrics['without_assignee_fact_ablation']['inspections_for_three_actionable'] or 'n/a'} |"
                ),
                (
                    f"| current graph | {metrics['product_available_ranking']['candidate_count']} | "
                    f"{metrics['false_available_count']} ({_percent(metrics['false_available_rate'])}) | "
                    f"{_percent(metrics['product_available_ranking']['precision_at_k'])} | "
                    f"{metrics['product_available_ranking']['inspections_for_three_actionable'] or 'n/a'} |"
                ),
                "",
            ]
        )
        if result["false_available_examples"]:
            lines.extend(["False-available examples:", ""])
            for item in result["false_available_examples"][:5]:
                reasons = "; ".join(item["oracle_reasons"])
                lines.append(
                    f"- [#{item['number']} {item['title']}]({item['url']}): {reasons}."
                )
            lines.append("")
        if result["oracle_actionable_not_returned_examples"]:
            lines.extend(
                [
                    "Oracle-actionable items withheld by conservative graph signals:",
                    "",
                ]
            )
            for item in result["oracle_actionable_not_returned_examples"][:5]:
                reasons = "; ".join(item["system_reasons"])
                lines.append(
                    f"- [#{item['number']} {item['title']}]({item['url']}): {reasons}."
                )
            lines.extend(
                [
                    "",
                    "These are not automatically counted as product errors: the explicit oracle may",
                    "be incomplete. They are the required human-review set for the next pilot stage.",
                    "",
                ]
            )
    lines.extend(
        [
            "## What remains unproven",
            "",
            "- Time-to-selection needs a timed A/B task with contributors; inspection depth is only a proxy.",
            "- Maintainer burden needs maintainer feedback; this run proves only that repository writes are zero.",
            "- The snapshot samples recent open items and the latest repository-wide comments, so it can miss",
            "  old comments or PRs outside the sample. A GitHub App backfill is not part of Pilot 0.",
            "- A plain PR reference is ambiguous. Only closing keywords count as oracle evidence; ambiguous",
            "  references are listed for manual review instead of being declared right or wrong.",
            "- Pilot 0 uses the deterministic GitHub layer only. Semantic fit and personalization require a",
            "  separate evaluation after factual availability is reliable.",
            "",
        ]
    )
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"
