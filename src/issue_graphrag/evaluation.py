from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import networkx as nx

from issue_graphrag.models import CommunityReport, SearchResult, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import naive_search


DEFAULT_MODES = ["naive", "local", "global"]
CATEGORY_DEFAULT_MODES = {
    "single_lookup": ["naive", "local"],
    "local_relationship": ["naive", "local"],
    "global_theme": ["naive", "local", "global"],
}


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    question: str
    expected_entities: list[str] = field(default_factory=list)
    expected_source_documents: list[str] = field(default_factory=list)
    expected_community_entities: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalTrace:
    results: list[SearchResult]
    context: str
    source_document_ids: list[str]
    community_ids: list[str]
    latency_ms: float


@dataclass(frozen=True)
class EvalRow:
    query_id: str
    category: str
    mode: str
    entity_recall: float | None
    source_recall_at_k: float | None
    source_mrr: float | None
    community_recall: float | None
    community_mrr: float | None
    latency_ms: float
    noise_entities: int
    missing_entities: list[str]
    missing_source_documents: list[str]
    retrieved_source_documents: list[str]
    retrieved_community_ids: list[str]


def normalize_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def contains_term(text: str, term: str) -> bool:
    normalized_term = normalize_text(term)
    return bool(normalized_term) and normalized_term in normalize_text(text)


def load_eval_cases(path: Path) -> list[EvalCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Evaluation file must contain a JSON list")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for item in raw:
        case = EvalCase(
            id=str(item["id"]),
            category=str(item.get("category", "uncategorized")),
            question=str(item["question"]),
            expected_entities=list(item.get("expected_entities", [])),
            expected_source_documents=list(item.get("expected_source_documents", [])),
            expected_community_entities=list(item.get("expected_community_entities", [])),
        )
        if case.id in seen_ids:
            raise ValueError(f"Duplicate evaluation case id: {case.id}")
        if not case.question.strip():
            raise ValueError(f"Evaluation case {case.id!r} has an empty question")
        if not (
            case.expected_entities
            or case.expected_source_documents
            or case.expected_community_entities
        ):
            raise ValueError(f"Evaluation case {case.id!r} has no expected evidence")
        seen_ids.add(case.id)
        cases.append(case)
    return cases


def modes_for_case(
    case: EvalCase,
    requested_modes: list[str],
    all_modes: bool,
) -> list[str]:
    if all_modes:
        return requested_modes
    category_modes = CATEGORY_DEFAULT_MODES.get(case.category, ["naive", "local"])
    return [mode for mode in requested_modes if mode in category_modes]


def _run_retrieval(
    mode: str,
    question: str,
    graph: nx.Graph,
    text_units: list[TextUnit],
    reports: list[CommunityReport],
    top_k: int,
) -> list[SearchResult]:
    if mode == "naive":
        return naive_search(text_units, question, top_k=top_k)
    if mode == "local":
        return local_search(graph, reports, text_units, question, top_k=top_k)
    if mode == "global":
        return global_search(reports, question, top_k=top_k)
    raise ValueError(f"Unsupported mode: {mode}")


def _context_from_results(results: list[SearchResult]) -> str:
    return "\n\n".join(result.text for result in results)


def _section(context: str, start: str, end: str | None = None) -> str:
    if start not in context:
        return ""
    value = context.split(start, maxsplit=1)[1]
    if end and end in value:
        value = value.split(end, maxsplit=1)[0]
    return value


def _bracketed_ids(text: str) -> list[str]:
    return re.findall(r"^\[([^\]]+)\]", text, flags=re.MULTILINE)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _source_unit_ids(mode: str, results: list[SearchResult], context: str) -> list[str]:
    if mode == "naive":
        return [result.id for result in results]
    if mode == "local":
        sources = _section(context, "-----Sources-----")
        return _bracketed_ids(sources)
    return []


def _community_ids(mode: str, context: str) -> list[str]:
    if mode == "local":
        reports = _section(context, "-----Reports-----", "-----Entities-----")
        return _bracketed_ids(reports)
    if mode == "global":
        return _bracketed_ids(context)
    return []


def _source_documents_from_units(
    unit_ids: Iterable[str],
    units_by_id: dict[str, TextUnit],
) -> list[str]:
    return _unique(
        units_by_id[unit_id].document_id
        for unit_id in unit_ids
        if unit_id in units_by_id
    )


def _source_documents_from_reports(
    community_ids: Iterable[str],
    reports_by_id: dict[str, CommunityReport],
    units_by_id: dict[str, TextUnit],
) -> list[str]:
    document_ids: list[str] = []
    for community_id in community_ids:
        report = reports_by_id.get(community_id)
        if not report:
            continue
        document_ids.extend(
            units_by_id[source_id].document_id
            for source_id in report.source_ids
            if source_id in units_by_id
        )
    return _unique(document_ids)


def trace_retrieval(
    mode: str,
    question: str,
    graph: nx.Graph,
    text_units: list[TextUnit],
    reports: list[CommunityReport],
    top_k: int,
    repeats: int = 1,
    retrieve: Callable[..., list[SearchResult]] = _run_retrieval,
) -> RetrievalTrace:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")

    durations: list[float] = []
    results: list[SearchResult] = []
    for _ in range(repeats):
        started = time.perf_counter()
        results = retrieve(mode, question, graph, text_units, reports, top_k)
        durations.append((time.perf_counter() - started) * 1000)

    context = _context_from_results(results)
    community_ids = _community_ids(mode, context)
    units_by_id = {unit.id: unit for unit in text_units}
    reports_by_id = {report.id: report for report in reports}
    source_document_ids = _source_documents_from_units(
        _source_unit_ids(mode, results, context),
        units_by_id,
    )
    if mode == "global":
        source_document_ids = _source_documents_from_reports(
            community_ids,
            reports_by_id,
            units_by_id,
        )

    return RetrievalTrace(
        results=results,
        context=context,
        source_document_ids=source_document_ids,
        community_ids=community_ids,
        latency_ms=statistics.median(durations),
    )


def recall_at_k(expected: Iterable[str], retrieved: Iterable[str], top_k: int) -> float | None:
    expected_set = set(expected)
    if not expected_set:
        return None
    retrieved_set = set(list(retrieved)[:top_k])
    return len(expected_set & retrieved_set) / len(expected_set)


def reciprocal_rank(expected: Iterable[str], retrieved: Iterable[str]) -> float | None:
    expected_set = set(expected)
    if not expected_set:
        return None
    for rank, value in enumerate(retrieved, start=1):
        if value in expected_set:
            return 1.0 / rank
    return 0.0


def _report_text(report: CommunityReport) -> str:
    return f"{report.title} {' '.join(report.entity_names)} {report.summary}"


def _community_metrics(
    expected_entities: list[str],
    community_ids: list[str],
    reports: list[CommunityReport],
) -> tuple[float | None, float | None]:
    if not expected_entities:
        return None, None

    reports_by_id = {report.id: report for report in reports}
    selected = [reports_by_id[cid] for cid in community_ids if cid in reports_by_id]
    combined = " ".join(_report_text(report) for report in selected)
    hits = sum(1 for entity in expected_entities if contains_term(combined, entity))
    recall = hits / len(expected_entities)

    first_relevant_rank: int | None = None
    for rank, report in enumerate(selected, start=1):
        text = _report_text(report)
        if any(contains_term(text, entity) for entity in expected_entities):
            first_relevant_rank = rank
            break
    mrr = 1.0 / first_relevant_rank if first_relevant_rank else 0.0
    return recall, mrr


def _count_noise_entities(
    context: str,
    graph: nx.Graph,
    expected_entities: list[str],
) -> int:
    expected_normalized = {normalize_text(entity) for entity in expected_entities}
    matched_noise: set[str] = set()
    for node in graph.nodes:
        node_name = str(node)
        normalized = normalize_text(node_name)
        if len(normalized) < 3 or normalized in expected_normalized:
            continue
        if contains_term(context, node_name):
            matched_noise.add(node_name)
    return len(matched_noise)


def evaluate_case(
    case: EvalCase,
    mode: str,
    graph: nx.Graph,
    text_units: list[TextUnit],
    reports: list[CommunityReport],
    top_k: int,
    repeats: int = 1,
    retrieve: Callable[..., list[SearchResult]] = _run_retrieval,
) -> EvalRow:
    trace = trace_retrieval(
        mode,
        case.question,
        graph,
        text_units,
        reports,
        top_k,
        repeats=repeats,
        retrieve=retrieve,
    )

    found_entities = [
        entity for entity in case.expected_entities if contains_term(trace.context, entity)
    ]
    missing_entities = [
        entity for entity in case.expected_entities if entity not in found_entities
    ]
    entity_recall = (
        len(found_entities) / len(case.expected_entities)
        if case.expected_entities
        else None
    )
    expected_sources = set(case.expected_source_documents)
    source_documents_for_recall = (
        trace.source_document_ids
        if mode == "global"
        else trace.source_document_ids[:top_k]
    )
    missing_sources = [
        source_id
        for source_id in case.expected_source_documents
        if source_id not in set(source_documents_for_recall)
    ]
    community_recall, community_mrr = _community_metrics(
        case.expected_community_entities,
        trace.community_ids,
        reports,
    )

    return EvalRow(
        query_id=case.id,
        category=case.category,
        mode=mode,
        entity_recall=entity_recall,
        source_recall_at_k=recall_at_k(
            expected_sources,
            source_documents_for_recall,
            len(source_documents_for_recall),
        ),
        # Global results rank reports rather than individual source documents.
        # A source MRR would depend on arbitrary source ordering inside each report.
        source_mrr=(
            None
            if mode == "global"
            else reciprocal_rank(expected_sources, trace.source_document_ids)
        ),
        community_recall=community_recall,
        community_mrr=community_mrr,
        latency_ms=trace.latency_ms,
        noise_entities=_count_noise_entities(trace.context, graph, case.expected_entities),
        missing_entities=missing_entities,
        missing_source_documents=missing_sources,
        retrieved_source_documents=source_documents_for_recall,
        retrieved_community_ids=trace.community_ids,
    )


def mean_optional(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def summarize(rows: list[EvalRow]) -> list[dict[str, float | int | str | None]]:
    by_mode: dict[str, list[EvalRow]] = {}
    for row in rows:
        by_mode.setdefault(row.mode, []).append(row)

    summary: list[dict[str, float | int | str | None]] = []
    for mode, mode_rows in sorted(by_mode.items()):
        summary.append(
            {
                "mode": mode,
                "queries": len(mode_rows),
                "entity_recall": mean_optional(row.entity_recall for row in mode_rows),
                "source_recall_at_k": mean_optional(
                    row.source_recall_at_k for row in mode_rows
                ),
                "source_mrr": mean_optional(row.source_mrr for row in mode_rows),
                "community_recall": mean_optional(
                    row.community_recall for row in mode_rows
                ),
                "community_mrr": mean_optional(row.community_mrr for row in mode_rows),
                "median_latency_ms": statistics.median(
                    row.latency_ms for row in mode_rows
                ),
                "avg_noise_entities": sum(
                    row.noise_entities for row in mode_rows
                ) / len(mode_rows),
            }
        )
    return summary
