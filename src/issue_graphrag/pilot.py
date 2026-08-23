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
from pathlib import Path
from typing import Any, Mapping

import requests

from issue_graphrag.ingest.github_loader import parse_repo, to_seed_item
from issue_graphrag.live.contribution import HELP_LABELS, opportunities
from issue_graphrag.live.facts import github_closing_numbers, github_reference_numbers
from issue_graphrag.live.indexer import NullExtractor, bootstrap
from issue_graphrag.live.models import Opportunity
from issue_graphrag.live.projection import project_graph
from issue_graphrag.live.records import seed_items
from issue_graphrag.live.timeutil import now_utc, parse_iso, to_iso

DEFAULT_PILOT_REPOS = (
    "getzep/graphiti",
    "pydantic/pydantic-ai",
    "trustgraph-ai/trustgraph",
)
DEFAULT_CONSTRAINT_CONTRADICTION_THRESHOLD = 0.05
PILOT_SNAPSHOT_SCHEMA_VERSION = 1
CONTRIBUTION_REGRESSION_SCHEMA_VERSION = 1

_ISSUE_URL_NUMBER = re.compile(r"/issues/(\d+)$")


@dataclass(frozen=True)
class PilotSnapshot:
    repo: str
    fetched_at: str
    raw_items: list[dict[str, Any]]
    recent_comments: list[dict[str, Any]]
    seed: dict[str, Any]
    request_count: int
    write_request_count: int
    fingerprint: str

    @property
    def issues(self) -> list[dict[str, Any]]:
        return [item for item in self.raw_items if "pull_request" not in item]

    @property
    def pulls(self) -> list[dict[str, Any]]:
        return [item for item in self.raw_items if "pull_request" in item]


def monitoring_run_id(value: str) -> str:
    """Turn a UTC timestamp into a filename-safe, sortable monitoring id."""
    return parse_iso(value).strftime("%Y%m%dT%H%M%SZ")


def create_monitoring_run_directory(parent: Path, value: str) -> Path:
    """Reserve an immutable directory for one time-varying pilot run."""
    destination = parent / monitoring_run_id(value)
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def _compact_actor(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"login": ""}
    return {"login": str(value.get("login") or "")}


def _compact_raw_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Retain the public GitHub fields that can affect Pilot 0 or its seed."""
    item = {
        key: raw.get(key)
        for key in (
            "number",
            "title",
            "body",
            "state",
            "locked",
            "draft",
            "merged",
            "created_at",
            "updated_at",
            "closed_at",
            "merged_at",
            "html_url",
            "comments",
            "issue_dependencies_summary",
        )
        if key in raw
    }
    item["labels"] = [
        {"name": str(label.get("name") or "")}
        for label in raw.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ]
    item["assignees"] = [
        _compact_actor(assignee)
        for assignee in raw.get("assignees") or []
        if isinstance(assignee, dict) and assignee.get("login")
    ]
    item["user"] = _compact_actor(raw.get("user"))
    if "pull_request" in raw:
        # Presence, not the nested API URLs, distinguishes PR rows returned by
        # GitHub's shared issues endpoint.
        item["pull_request"] = {}
    return item


def _compact_comment(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("id"),
        "issue_url": raw.get("issue_url"),
        "body": raw.get("body"),
        "user": _compact_actor(raw.get("user")),
        "html_url": raw.get("html_url"),
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }


def snapshot_to_payload(
    snapshot: PilotSnapshot,
    *,
    collection_parameters: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize a compact, replayable Pilot 0 snapshot with provenance."""
    raw_items = [_compact_raw_item(raw) for raw in snapshot.raw_items]
    recent_comments = [_compact_comment(raw) for raw in snapshot.recent_comments]
    compacted = make_snapshot(
        snapshot.repo,
        raw_items,
        recent_comments,
        fetched_at=snapshot.fetched_at,
        request_count=snapshot.request_count,
        write_request_count=snapshot.write_request_count,
    )
    if compacted.fingerprint != snapshot.fingerprint:
        raise ValueError("compacting the pilot snapshot changed its fingerprint")
    return {
        "schema_version": PILOT_SNAPSHOT_SCHEMA_VERSION,
        "source": {
            "provider": "GitHub REST API",
            "repo": snapshot.repo,
            "api_url": f"https://api.github.com/repos/{snapshot.repo}",
            "fetched_at": snapshot.fetched_at,
        },
        "collection": {
            "github_read_requests": snapshot.request_count,
            "github_write_requests": snapshot.write_request_count,
            "parameters": dict(collection_parameters or {}),
        },
        "snapshot_fingerprint": snapshot.fingerprint,
        "raw_items": raw_items,
        "recent_comments": recent_comments,
    }


def snapshot_from_payload(payload: Mapping[str, Any]) -> PilotSnapshot:
    """Restore and integrity-check a snapshot produced by ``snapshot_to_payload``."""
    if payload.get("schema_version") != PILOT_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("unsupported pilot snapshot schema_version")
    source = payload.get("source")
    collection = payload.get("collection")
    raw_items = payload.get("raw_items")
    recent_comments = payload.get("recent_comments")
    if not isinstance(source, dict) or not isinstance(collection, dict):
        raise ValueError("pilot snapshot source and collection must be objects")
    if not isinstance(raw_items, list) or not all(isinstance(row, dict) for row in raw_items):
        raise ValueError("pilot snapshot raw_items must be a list of objects")
    if not isinstance(recent_comments, list) or not all(
        isinstance(row, dict) for row in recent_comments
    ):
        raise ValueError("pilot snapshot recent_comments must be a list of objects")

    snapshot = make_snapshot(
        str(source.get("repo") or ""),
        raw_items,
        recent_comments,
        fetched_at=str(source.get("fetched_at") or ""),
        request_count=int(collection.get("github_read_requests") or 0),
        write_request_count=int(collection.get("github_write_requests") or 0),
    )
    expected = str(payload.get("snapshot_fingerprint") or "")
    if snapshot.fingerprint != expected:
        raise ValueError(
            "pilot snapshot fingerprint mismatch: "
            f"expected {expected or '<missing>'}, calculated {snapshot.fingerprint}"
        )
    return snapshot


class CountingSession:
    """Count read and non-GET requests at the HTTP boundary.

    The write count is intentionally attached to the session instead of being
    a report literal. A later POST/PUT/PATCH/DELETE therefore makes the
    read-only check fail even if a caller forgets to update the evaluator.
    """

    def __init__(self, session: Any):
        self._session = session
        self.read_count = 0
        self.write_count = 0

    def _count(self, method: str) -> None:
        if method.upper() == "GET":
            self.read_count += 1
        else:
            self.write_count += 1

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self._count(method)
        return self._session.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        self._count("GET")
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        self._count("POST")
        return self._session.post(url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        self._count("PUT")
        return self._session.put(url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        self._count("PATCH")
        return self._session.patch(url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        self._count("DELETE")
        return self._session.delete(url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


class GitHubPilotClient:
    """A read-only GitHub client whose HTTP method counts are auditable."""

    def __init__(
        self,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout_seconds: float = 30.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.token = token
        self.session = CountingSession(session or requests.Session())
        self.timeout_seconds = timeout_seconds

    @property
    def request_count(self) -> int:
        return self.session.read_count

    @property
    def write_request_count(self) -> int:
        return self.session.write_count

    def _get_list(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-issue-graphrag-pilot",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
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
        start_write_requests = self.write_request_count
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
            write_request_count=self.write_request_count - start_write_requests,
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
    write_request_count: int = 0,
) -> PilotSnapshot:
    """Build the same seed used by the live index while retaining platform signals."""
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
        write_request_count=write_request_count,
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


def _pr_links(snapshot: PilotSnapshot) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    issue_numbers = {int(raw["number"]) for raw in snapshot.issues}
    strong: dict[int, list[int]] = {}
    weak: dict[int, list[int]] = {}
    for pull in snapshot.pulls:
        pull_number = int(pull["number"])
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        # These intentionally use the production parser. Closing-PR labels are
        # therefore an integration-consistency signal, not independent truth
        # about the parser's accuracy.
        strong_numbers = github_closing_numbers(text, snapshot.repo)
        reference_numbers = github_reference_numbers(text, snapshot.repo)
        for number in sorted(strong_numbers & issue_numbers):
            strong.setdefault(number, []).append(pull_number)
        for number in sorted((reference_numbers - strong_numbers) & issue_numbers):
            weak.setdefault(number, []).append(pull_number)
    return strong, weak


def _constraint_reasons(raw: dict[str, Any], strong_claims: list[int]) -> list[str]:
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
    constraint_clear: set[int],
    top_k: int,
    target_choices: int = 3,
) -> dict[str, Any]:
    top = numbers[:top_k]
    # Missing slots count as misses. Otherwise a product returning a single
    # perfect candidate would report the same P@10 as one returning ten.
    precision = sum(number in constraint_clear for number in top) / top_k
    found = 0
    inspections: int | None = None
    for rank, number in enumerate(numbers, start=1):
        if number in constraint_clear:
            found += 1
            if found == target_choices:
                inspections = rank
                break
    return {
        "candidate_count": len(numbers),
        "returned_at_k": len(top),
        "constraint_clear_precision_at_k": round(precision, 4),
        "inspections_for_three_constraint_clear": inspections,
        "top_numbers": top,
    }


def _issue_summary(
    raw: dict[str, Any],
    status: str | None,
    score: float | None,
    constraint_reasons: list[str],
) -> dict[str, Any]:
    return {
        "number": int(raw["number"]),
        "title": str(raw.get("title") or ""),
        "url": raw.get("html_url"),
        "labels": _labels(raw),
        "assignees": _assignees(raw),
        "system_status": status,
        "system_score": score,
        "platform_constraint_clear": not constraint_reasons,
        "platform_constraint_reasons": constraint_reasons,
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


def contribution_regression_signature(snapshot: PilotSnapshot) -> dict[str, Any]:
    """Return the exact deterministic recommendation contract for a fixture.

    This deliberately excludes time-varying Pilot 0 comparison metrics. A
    checked-in golden file should fail when production status, score, reasons,
    evidence or ordering changes, forcing an explicit review of the new
    expected contract.
    """
    ranked = _rank_snapshot(snapshot, include_assignees=True)
    return {
        "schema_version": CONTRIBUTION_REGRESSION_SCHEMA_VERSION,
        "repo": snapshot.repo,
        "snapshot_fingerprint": snapshot.fingerprint,
        "opportunities": [
            {
                "number": item.number,
                "title": item.title,
                "status": item.status,
                "score": item.score,
                "reasons": item.reasons,
                "evidence": [row.model_dump(mode="json") for row in item.evidence],
            }
            for item in ranked
        ],
    }


def evaluate_snapshot(
    snapshot: PilotSnapshot,
    top_k: int = 10,
    constraint_contradiction_threshold: float = (
        DEFAULT_CONSTRAINT_CONTRADICTION_THRESHOLD
    ),
) -> dict[str, Any]:
    """Check ranking consistency with explicit GitHub platform constraints.

    This is deliberately not called an independent oracle. Assignee facts and
    closing-keyword links overlap production behavior; lock and native
    dependency fields are the only platform constraints not modeled by the
    current product.
    """
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if not 0 <= constraint_contradiction_threshold <= 1:
        raise ValueError("constraint_contradiction_threshold must be between 0 and 1")

    ranked = _rank_snapshot(snapshot, include_assignees=True)
    without_assignee_facts = _rank_snapshot(snapshot, include_assignees=False)
    by_number = {item.number: item for item in ranked}
    raw_by_number = {int(raw["number"]): raw for raw in snapshot.issues}
    strong_claims, weak_claims = _pr_links(snapshot)

    reasons_by_number = {
        number: _constraint_reasons(raw, strong_claims.get(number, []))
        for number, raw in raw_by_number.items()
    }
    constraint_clear = {
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

    contradiction_numbers = [
        number for number in product_numbers if reasons_by_number.get(number)
    ]
    contradiction_rate = (
        len(contradiction_numbers) / len(product_numbers) if product_numbers else None
    )
    ablation_contradiction_numbers = [
        number for number in ablation_numbers if reasons_by_number.get(number)
    ]
    ablation_contradiction_rate = (
        len(ablation_contradiction_numbers) / len(ablation_numbers)
        if ablation_numbers
        else None
    )

    product_set = set(product_numbers)
    returned_constraint_clear = product_set & constraint_clear
    constraint_clear_coverage = (
        len(returned_constraint_clear) / len(constraint_clear)
        if constraint_clear
        else None
    )
    unreturned_constraint_clear = [
        item.number
        for item in ranked
        if item.number in constraint_clear and item.number not in product_set
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

    product_metrics = _ranking_metrics(product_numbers, constraint_clear, top_k)
    ablation_metrics = _ranking_metrics(ablation_numbers, constraint_clear, top_k)
    ablation_metrics.update(
        {
            "platform_constraint_contradiction_count": len(
                ablation_contradiction_numbers
            ),
            "platform_constraint_contradiction_rate": (
                round(ablation_contradiction_rate, 4)
                if ablation_contradiction_rate is not None
                else None
            ),
        }
    )
    recent_metrics = _ranking_metrics(recent_numbers, constraint_clear, top_k)
    curated_metrics = _ranking_metrics(curated_numbers, constraint_clear, top_k)
    contradiction_rate_pass = (
        contradiction_rate is not None
        and contradiction_rate <= constraint_contradiction_threshold
    )

    contradiction_examples = [
        _issue_summary(
            raw_by_number[number],
            by_number[number].status if number in by_number else None,
            by_number[number].score if number in by_number else None,
            reasons_by_number[number],
        )
        for number in contradiction_numbers[:10]
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
                "no sampled platform constraint fired; conservative system evidence is an "
                "optional future-review candidate"
            ),
        }
        for number in unreturned_constraint_clear[:10]
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
            "github_write_requests": snapshot.write_request_count,
            "reported_comment_count_on_sampled_items": sum(
                int(raw.get("comments") or 0) for raw in snapshot.raw_items
            ),
        },
        "system_status_counts": {
            status: sum(item.status == status for item in ranked)
            for status in ("available", "claimed", "blocked")
        },
        "platform_constraints": {
            "clear": len(constraint_clear),
            "flagged": len(raw_by_number) - len(constraint_clear),
            "assigned": sum(bool(_assignees(raw)) for raw in snapshot.issues),
            "locked": sum(bool(raw.get("locked")) for raw in snapshot.issues),
            "blocked_by_dependency": sum(_blocked_by_count(raw) > 0 for raw in snapshot.issues),
            "claimed_by_closing_pr": len(strong_claims),
            "measurement_basis": {
                "raw_github_fields": [
                    "assignees",
                    "locked",
                    "issue_dependencies_summary",
                ],
                "shared_production_parser": ["pull request closing-keyword references"],
            },
        },
        "metrics": {
            "product_available_ranking": product_metrics,
            "without_assignee_fact_ablation": ablation_metrics,
            "github_recent_open_baseline": recent_metrics,
            "github_unassigned_curated_baseline": curated_metrics,
            "platform_constraint_contradiction_count": len(contradiction_numbers),
            "platform_constraint_contradiction_rate": (
                round(contradiction_rate, 4) if contradiction_rate is not None else None
            ),
            "causal_evidence_url_coverage": round(evidence_coverage, 4),
            "ambiguous_plain_reference_claims": len(ambiguous_claims),
            "constraint_clear_coverage": (
                round(constraint_clear_coverage, 4)
                if constraint_clear_coverage is not None
                else None
            ),
            "constraint_clear_not_returned_count": len(unreturned_constraint_clear),
        },
        "engineering_checks": {
            "constraint_contradiction_rate_at_most": constraint_contradiction_threshold,
            "constraint_contradiction_rate_pass": contradiction_rate_pass,
            "all_non_available_results_have_causal_evidence_url": evidence_coverage == 1.0,
            "github_write_requests_are_zero": snapshot.write_request_count == 0,
            "higher_constraint_clear_precision_than_recent_at_k": (
                product_metrics["constraint_clear_precision_at_k"]
                > recent_metrics["constraint_clear_precision_at_k"]
            ),
        },
        "top_product_candidates": top_product,
        "platform_constraint_contradiction_examples": contradiction_examples,
        "ambiguous_claim_examples": ambiguous_examples,
        "constraint_clear_not_returned_examples": unreturned_examples,
    }


def render_markdown(results: list[dict[str, Any]], top_k: int) -> str:
    """Render a compact, honest report suitable for committing with the run."""
    lines = [
        "# Real-repository contribution pilot",
        "",
        "Pilot 0 is a read-only engineering consistency and coverage evaluation, not a user study",
        "or an independent recommendation-quality benchmark. It does **not** prove that contributors",
        "work faster or that maintainers perceive less burden.",
        "",
        "## Summary",
        "",
        (
            f"| repository | issues | PRs | API GETs | available ∩ constrained | "
            f"product clear P@{top_k} | clear coverage | recent clear P@{top_k} | "
            f"curated clear P@{top_k} |"
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
            f"{metrics['platform_constraint_contradiction_count']} "
            f"({_percent(metrics['platform_constraint_contradiction_rate'])}) | "
            f"{_percent(metrics['product_available_ranking']['constraint_clear_precision_at_k'])} | "
            f"{_percent(metrics['constraint_clear_coverage'])} | "
            f"{_percent(metrics['github_recent_open_baseline']['constraint_clear_precision_at_k'])} | "
            f"{_percent(metrics['github_unassigned_curated_baseline']['constraint_clear_precision_at_k'])} |"
        )

    lines.extend(
        [
            "",
            "The curated baseline uses only native GitHub fields: no assignee, newcomer labels first,",
            "then recent update order. A platform constraint flags an assignee, a locked conversation,",
            "a native GitHub dependency, or an open PR using a closing keyword. ‘Clear’ means only that",
            "none of those sampled signals fired; it does not mean a person judged the issue suitable.",
            "",
            "This is not an independent oracle. Assignee and closing-PR signals overlap production",
            "behavior, and closing keywords deliberately use the exact production parser. They test",
            "integration consistency. Only lock and native-dependency fields can expose a constraint the",
            "current product does not model. Their observed counts are shown below.",
            "",
            f"All P@{top_k} values use {top_k} as the fixed denominator. Missing result slots count as",
            "misses, so returning one perfect candidate cannot score the same as returning ten.",
            "",
        ]
    )

    for result in results:
        metrics = result["metrics"]
        checks = result["engineering_checks"]
        collection = result["collection"]
        constraints = result["platform_constraints"]
        lines.extend(
            [
                f"## {result['repo']}",
                "",
                f"Snapshot: `{result['fetched_at']}`; fingerprint `{result['snapshot_fingerprint']}`.",
                f"GitHub operations counted at the HTTP boundary: "
                f"{collection['github_read_requests']} GET, "
                f"**{collection['github_write_requests']} writes**.",
                f"Platform-only exposure: {constraints['locked']} locked issue(s), "
                f"{constraints['blocked_by_dependency']} native-dependency issue(s).",
                "",
                f"| ranking | candidates | returned / {top_k} | clear P@{top_k} | "
                "inspections for 3 clear |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for label, key in (
            ("product available", "product_available_ranking"),
            ("GitHub recent", "github_recent_open_baseline"),
            ("GitHub curated", "github_unassigned_curated_baseline"),
        ):
            row = metrics[key]
            lines.append(
                f"| {label} | {row['candidate_count']} | {row['returned_at_k']} | "
                f"{_percent(row['constraint_clear_precision_at_k'])} | "
                f"{row['inspections_for_three_constraint_clear'] or 'n/a'} |"
            )
        lines.extend(
            [
                "",
                f"Engineering contradiction threshold: "
                f"{_percent(checks['constraint_contradiction_rate_at_most'])}; "
                f"result **{'PASS' if checks['constraint_contradiction_rate_pass'] else 'FAIL'}**.",
                "This is a consistency check, not an estimate of recommendation accuracy.",
                "",
                "Assignee-fact ablation on this exact snapshot:",
                "",
                f"| treatment | available candidates | constraint contradictions | clear P@{top_k} | "
                "inspections for 3 clear |",
                "|---|---:|---:|---:|---:|",
                (
                    f"| assignee facts suppressed | "
                    f"{metrics['without_assignee_fact_ablation']['candidate_count']} | "
                    f"{metrics['without_assignee_fact_ablation']['platform_constraint_contradiction_count']} "
                    f"({_percent(metrics['without_assignee_fact_ablation']['platform_constraint_contradiction_rate'])}) | "
                    f"{_percent(metrics['without_assignee_fact_ablation']['constraint_clear_precision_at_k'])} | "
                    f"{metrics['without_assignee_fact_ablation']['inspections_for_three_constraint_clear'] or 'n/a'} |"
                ),
                (
                    f"| current graph | {metrics['product_available_ranking']['candidate_count']} | "
                    f"{metrics['platform_constraint_contradiction_count']} "
                    f"({_percent(metrics['platform_constraint_contradiction_rate'])}) | "
                    f"{_percent(metrics['product_available_ranking']['constraint_clear_precision_at_k'])} | "
                    f"{metrics['product_available_ranking']['inspections_for_three_constraint_clear'] or 'n/a'} |"
                ),
                "",
            ]
        )
        if result["platform_constraint_contradiction_examples"]:
            lines.extend(["Available results that contradict platform constraints:", ""])
            for item in result["platform_constraint_contradiction_examples"][:5]:
                reasons = "; ".join(item["platform_constraint_reasons"])
                lines.append(
                    f"- [#{item['number']} {item['title']}]({item['url']}): {reasons}."
                )
            lines.append("")
        if result["constraint_clear_not_returned_examples"]:
            lines.extend(
                [
                    "Constraint-clear items withheld by conservative graph signals:",
                    "",
                ]
            )
            for item in result["constraint_clear_not_returned_examples"][:5]:
                reasons = "; ".join(item["system_reasons"])
                lines.append(
                    f"- [#{item['number']} {item['title']}]({item['url']}): {reasons}."
                )
            lines.extend(
                [
                    "",
                    "These are not automatically product errors: absence of a sampled platform",
                    "constraint is not human validation. They are optional future-review candidates.",
                    "",
                ]
            )
    lines.extend(
        [
            "## What remains unproven",
            "",
            "- Time-to-selection needs a timed A/B task with contributors; the inspection count is only a proxy.",
            "  Pilot 0 does not require recruiting participants because it makes no human-outcome claim.",
            "- Maintainer burden needs maintainer feedback; this run proves only that its measured write count is zero.",
            "- The snapshot samples recent open items and the latest repository-wide comments, so it can miss",
            "  old comments or PRs outside the sample. A GitHub App backfill is not part of Pilot 0.",
            "- A plain PR reference is ambiguous. Closing keywords count as a platform constraint; ambiguous",
            "  references are listed for manual review instead of being declared right or wrong.",
            "- Because the constraint evaluator shares the production closing parser, it cannot measure that",
            "  parser's accuracy. Parser behavior is covered by tests, not by these live headline metrics.",
            "- Pilot 0 uses the deterministic GitHub layer only. Semantic fit and personalization require a",
            "  separate evaluation after factual availability is reliable.",
            "",
        ]
    )
    return "\n".join(lines)


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"
