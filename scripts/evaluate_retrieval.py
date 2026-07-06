from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from issue_graphrag.config import load_settings
from issue_graphrag.models import CommunityReport, SearchResult, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import naive_search
from issue_graphrag.storage.json_store import read_graph, read_json


DEFAULT_MODES = ["naive", "local", "global"]
CATEGORY_DEFAULT_MODES = {
    "single_lookup": ["naive", "local"],
    "local_relationship": ["naive", "local"],
    "global_theme": ["naive", "local", "global"],
}

EVAL_NOTE = """\
Evaluation note:
- `naive` is the BM25 lexical baseline.
- `local` is the query-dependent local GraphRAG retrieval path.
- `global` returns top-rated community reports as global context; the query-aware global reasoning
  happens in the optional LLM map-reduce step, not in this pure retrieval evaluation.
- Therefore, by default this script evaluates `global` only for `global_theme` questions. Use
  `--all-modes` to force every mode on every query for debugging.
""".strip()


@dataclass
class EvalCase:
    id: str
    category: str
    question: str
    expected_entities: list[str]


@dataclass
class EvalRow:
    query_id: str
    category: str
    mode: str
    recall: float
    hits: int
    total: int
    noise_entities: int
    missing_entities: list[str]


def _normalize_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _contains_entity(context: str, entity: str) -> bool:
    return _normalize_text(entity) in _normalize_text(context)


def _load_eval_cases(path: Path) -> list[EvalCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[EvalCase] = []

    for item in raw:
        cases.append(
            EvalCase(
                id=item["id"],
                category=item.get("category", "uncategorized"),
                question=item["question"],
                expected_entities=list(item.get("expected_entities", [])),
            )
        )

    return cases


def _load_processed_data():
    settings = load_settings()
    processed = settings.processed_data_dir

    graph = read_graph(processed / "graph.json")
    text_units = [TextUnit.model_validate(x) for x in read_json(processed / "text_units.json")]
    reports = [CommunityReport.model_validate(x) for x in read_json(processed / "community_reports.json")]

    return graph, text_units, reports


def _modes_for_case(case: EvalCase, requested_modes: list[str], all_modes: bool) -> list[str]:
    if all_modes:
        return requested_modes

    default_for_category = CATEGORY_DEFAULT_MODES.get(case.category, ["naive", "local"])
    return [mode for mode in requested_modes if mode in default_for_category]


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


def _count_noise_entities(context: str, graph: nx.Graph, expected_entities: list[str]) -> int:
    expected_normalized = {_normalize_text(entity) for entity in expected_entities}
    matched_noise: set[str] = set()

    for node in graph.nodes:
        node_name = str(node)
        normalized = _normalize_text(node_name)

        # Skip tiny or overly generic node names so the noise proxy is less brittle.
        if len(normalized) < 3 or normalized in expected_normalized:
            continue

        if _contains_entity(context, node_name):
            matched_noise.add(node_name)

    return len(matched_noise)


def _evaluate_case(
    case: EvalCase,
    mode: str,
    graph: nx.Graph,
    text_units: list[TextUnit],
    reports: list[CommunityReport],
    top_k: int,
) -> EvalRow:
    results = _run_retrieval(mode, case.question, graph, text_units, reports, top_k=top_k)
    context = _context_from_results(results)

    found_entities = [
        entity for entity in case.expected_entities
        if _contains_entity(context, entity)
    ]
    missing_entities = [
        entity for entity in case.expected_entities
        if entity not in found_entities
    ]

    total = len(case.expected_entities)
    hits = len(found_entities)
    recall = hits / total if total else 0.0
    noise_entities = _count_noise_entities(context, graph, case.expected_entities)

    return EvalRow(
        query_id=case.id,
        category=case.category,
        mode=mode,
        recall=recall,
        hits=hits,
        total=total,
        noise_entities=noise_entities,
        missing_entities=missing_entities,
    )


def _summarize(rows: list[EvalRow]) -> list[dict[str, Any]]:
    by_mode: dict[str, list[EvalRow]] = {}
    for row in rows:
        by_mode.setdefault(row.mode, []).append(row)

    summary: list[dict[str, Any]] = []
    for mode, mode_rows in sorted(by_mode.items()):
        avg_recall = sum(row.recall for row in mode_rows) / len(mode_rows)
        avg_noise = sum(row.noise_entities for row in mode_rows) / len(mode_rows)
        perfect = sum(1 for row in mode_rows if row.recall == 1.0)
        summary.append(
            {
                "mode": mode,
                "queries": len(mode_rows),
                "avg_recall": avg_recall,
                "avg_noise_entities": avg_noise,
                "perfect_recall_queries": perfect,
            }
        )

    return summary


def _print_summary(summary: list[dict[str, Any]]) -> None:
    print("\n" + EVAL_NOTE + "\n")
    print("Summary by mode")
    print("| mode | queries | avg recall | avg noise entities | perfect recall queries |")
    print("|---|---:|---:|---:|---:|")
    for item in summary:
        print(
            f"| {item['mode']} | {item['queries']} | {item['avg_recall']:.3f} | "
            f"{item['avg_noise_entities']:.1f} | {item['perfect_recall_queries']} |"
        )


def _print_rows(rows: list[EvalRow]) -> None:
    print("\nDetailed results")
    print("| query | category | mode | recall | hits | noise | missing |")
    print("|---|---|---|---:|---:|---:|---|")
    for row in rows:
        missing = ", ".join(row.missing_entities) if row.missing_entities else "-"
        print(
            f"| {row.query_id} | {row.category} | {row.mode} | {row.recall:.3f} | "
            f"{row.hits}/{row.total} | {row.noise_entities} | {missing} |"
        )


def _write_csv(path: Path, rows: list[EvalRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "query_id",
                "category",
                "mode",
                "recall",
                "hits",
                "total",
                "noise_entities",
                "missing_entities",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "query_id": row.query_id,
                    "category": row.category,
                    "mode": row.mode,
                    "recall": f"{row.recall:.3f}",
                    "hits": row.hits,
                    "total": row.total,
                    "noise_entities": row.noise_entities,
                    "missing_entities": "; ".join(row.missing_entities),
                }
            )


def _write_markdown(path: Path, rows: list[EvalRow], summary: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Retrieval evaluation results")
    lines.append("")
    lines.append("This file is generated by `python scripts/evaluate_retrieval.py`.")
    lines.append("")
    lines.append("## Evaluation note")
    lines.append("")
    lines.extend(f"- {line}" for line in EVAL_NOTE.split("\n- "))
    lines.append("")
    lines.append("## Summary by mode")
    lines.append("")
    lines.append("| mode | queries | avg recall | avg noise entities | perfect recall queries |")
    lines.append("|---|---:|---:|---:|---:|")
    for item in summary:
        lines.append(
            f"| {item['mode']} | {item['queries']} | {item['avg_recall']:.3f} | "
            f"{item['avg_noise_entities']:.1f} | {item['perfect_recall_queries']} |"
        )

    lines.append("")
    lines.append("## Detailed results")
    lines.append("")
    lines.append("| query | category | mode | recall | hits | noise | missing |")
    lines.append("|---|---|---|---:|---:|---:|---|")
    for row in rows:
        missing = ", ".join(row.missing_entities) if row.missing_entities else "-"
        lines.append(
            f"| {row.query_id} | {row.category} | {row.mode} | {row.recall:.3f} | "
            f"{row.hits}/{row.total} | {row.noise_entities} | {missing} |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate retrieval context entity recall.")
    parser.add_argument("--queries", default="eval/queries.json")
    parser.add_argument("--modes", nargs="+", choices=DEFAULT_MODES, default=DEFAULT_MODES)
    parser.add_argument("--all-modes", action="store_true", help="Evaluate every requested mode on every query.")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--markdown-output", default="eval/results.md")
    parser.add_argument("--csv-output", default="eval/results.csv")
    args = parser.parse_args()

    cases = _load_eval_cases(Path(args.queries))
    graph, text_units, reports = _load_processed_data()

    rows: list[EvalRow] = []
    for case in cases:
        for mode in _modes_for_case(case, args.modes, args.all_modes):
            rows.append(
                _evaluate_case(
                    case=case,
                    mode=mode,
                    graph=graph,
                    text_units=text_units,
                    reports=reports,
                    top_k=args.top_k,
                )
            )

    summary = _summarize(rows)
    _print_summary(summary)
    _print_rows(rows)
    _write_markdown(Path(args.markdown_output), rows, summary)
    _write_csv(Path(args.csv_output), rows)

    print(f"\nWrote markdown results to {args.markdown_output}")
    print(f"Wrote CSV results to {args.csv_output}")


if __name__ == "__main__":
    main()
