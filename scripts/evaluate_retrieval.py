from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from issue_graphrag.config import load_settings
from issue_graphrag.evaluation import (
    DEFAULT_MODES,
    EvalRow,
    evaluate_case,
    load_eval_cases,
    modes_for_case,
    summarize,
)
from issue_graphrag.models import CommunityReport, TextUnit
from issue_graphrag.storage.json_store import read_graph, read_json


EVAL_NOTE = """\
Evaluation note:
- `naive` is the BM25 lexical baseline.
- `local` is the current query-dependent local GraphRAG path.
- `global` currently selects top-rated community reports without query-dependent ranking, so it is
  evaluated by default only for `global_theme` questions.
- Global source recall covers documents attached to the selected top-k reports; source MRR is not
  reported because source ordering inside a community report is not a retrieval ranking.
- Latency is end-to-end retrieval latency for the current implementation. It includes BM25 index
  construction because the existing naive path rebuilds BM25 for every query.
""".strip()


def _load_processed_data():
    settings = load_settings()
    processed = settings.processed_data_dir
    graph = read_graph(processed / "graph.json")
    text_units = [
        TextUnit.model_validate(item)
        for item in read_json(processed / "text_units.json")
    ]
    reports = [
        CommunityReport.model_validate(item)
        for item in read_json(processed / "community_reports.json")
    ]
    return graph, text_units, reports


def _display(value: float | int | str | None, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _summary_lines(summary: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| mode | queries | entity recall | source R@K | source MRR | community recall | community MRR | median ms | noise |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary:
        lines.append(
            f"| {item['mode']} | {item['queries']} | {_display(item['entity_recall'])} | "
            f"{_display(item['source_recall_at_k'])} | {_display(item['source_mrr'])} | "
            f"{_display(item['community_recall'])} | {_display(item['community_mrr'])} | "
            f"{_display(item['median_latency_ms'], 2)} | "
            f"{_display(item['avg_noise_entities'], 1)} |"
        )
    return lines


def _detail_lines(rows: list[EvalRow]) -> list[str]:
    lines = [
        "| query | category | mode | entity recall | source R@K | source MRR | community recall | ms | missing sources | missing entities |",
        "|---|---|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        missing_sources = ", ".join(row.missing_source_documents) or "-"
        missing_entities = ", ".join(row.missing_entities) or "-"
        lines.append(
            f"| {row.query_id} | {row.category} | {row.mode} | "
            f"{_display(row.entity_recall)} | {_display(row.source_recall_at_k)} | "
            f"{_display(row.source_mrr)} | {_display(row.community_recall)} | "
            f"{row.latency_ms:.2f} | {missing_sources} | {missing_entities} |"
        )
    return lines


def _write_csv(path: Path, rows: list[EvalRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(EvalRow.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            values = {field: getattr(row, field) for field in fields}
            for field in (
                "missing_entities",
                "missing_source_documents",
                "retrieved_source_documents",
                "retrieved_community_ids",
            ):
                values[field] = "; ".join(values[field])
            writer.writerow(values)


def _write_markdown(
    path: Path,
    rows: list[EvalRow],
    summary: list[dict[str, Any]],
    top_k: int,
    repeats: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Retrieval evaluation results",
        "",
        f"Configuration: `top_k={top_k}`, `repeats={repeats}`.",
        "",
        "## Evaluation note",
        "",
        EVAL_NOTE,
        "",
        "## Summary by mode",
        "",
        *_summary_lines(summary),
        "",
        "## Detailed results",
        "",
        *_detail_lines(rows),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate the existing retrieval paths against annotated evidence."
    )
    parser.add_argument("--queries", default="eval/queries.json")
    parser.add_argument("--modes", nargs="+", choices=DEFAULT_MODES, default=DEFAULT_MODES)
    parser.add_argument(
        "--all-modes",
        action="store_true",
        help="Evaluate every requested mode on every query.",
    )
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="Retrieval repetitions used for median latency.",
    )
    parser.add_argument("--markdown-output", default="eval/results.md")
    parser.add_argument("--csv-output", default="eval/results.csv")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    cases = load_eval_cases(Path(args.queries))
    graph, text_units, reports = _load_processed_data()
    rows: list[EvalRow] = []
    for case in cases:
        for mode in modes_for_case(case, args.modes, args.all_modes):
            rows.append(
                evaluate_case(
                    case,
                    mode,
                    graph,
                    text_units,
                    reports,
                    top_k=args.top_k,
                    repeats=args.repeats,
                )
            )

    summary = summarize(rows)
    print("\n" + EVAL_NOTE + "\n")
    print("Summary by mode")
    print("\n".join(_summary_lines(summary)))
    print("\nDetailed results")
    print("\n".join(_detail_lines(rows)))
    _write_markdown(Path(args.markdown_output), rows, summary, args.top_k, args.repeats)
    _write_csv(Path(args.csv_output), rows)
    print(f"\nWrote markdown results to {args.markdown_output}")
    print(f"Wrote CSV results to {args.csv_output}")


if __name__ == "__main__":
    main()
