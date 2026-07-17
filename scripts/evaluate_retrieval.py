from __future__ import annotations

import argparse
import csv
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Callable

from issue_graphrag.config import load_settings
from issue_graphrag.evaluation import (
    AVAILABLE_MODES,
    DEFAULT_MODES,
    EvalRow,
    evaluate_case,
    load_eval_cases,
    modes_for_case,
    summarize,
)
from issue_graphrag.models import CommunityReport, SearchResult, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.hybrid_search import (
    DEFAULT_FUSION_DEPTH,
    DEFAULT_RRF_K,
    HybridRetriever,
)
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import BM25Retriever
from issue_graphrag.retrieval.vector_search import VectorRetriever
from issue_graphrag.storage.json_store import read_graph, read_json


EVAL_NOTE = """\
Evaluation note:
- `naive` is the BM25 lexical baseline.
- `local` is the current query-dependent local GraphRAG path.
- `global` currently selects top-rated community reports without query-dependent ranking, so it is
  evaluated by default only for `global_theme` questions.
- Global source recall covers documents attached to the selected top-k reports; source MRR is not
  reported because source ordering inside a community report is not a retrieval ranking.
- `vector` ranks TextUnits from the prebuilt embedded Qdrant collection.
- `hybrid` fuses reusable BM25 and vector rankings with RRF; raw lexical and cosine scores are not
  mixed directly.
- Community metrics are reported only for `local` and `global`, which return ranked community
  reports. TextUnit modes report these metrics as `n/a`.
- Reported query latency reuses initialized retrievers. One-time BM25 construction, embedding
  model loading, and Qdrant opening are reported separately as setup timings.
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


def _setup_lines(setup_timings: dict[str, float]) -> list[str]:
    if not setup_timings:
        return ["No reusable retriever setup was required."]
    lines = ["| setup step | ms |", "|---|---:|"]
    lines.extend(
        f"| {name} | {duration_ms:.2f} |"
        for name, duration_ms in setup_timings.items()
    )
    return lines


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
    setup_timings: dict[str, float],
    fusion_depth: int,
    rrf_k: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Retrieval evaluation results",
        "",
        (
            f"Configuration: `top_k={top_k}`, `repeats={repeats}`, "
            f"`fusion_depth={fusion_depth}`, `rrf_k={rrf_k}`."
        ),
        "",
        "## Evaluation note",
        "",
        EVAL_NOTE,
        "",
        "## One-time setup timings",
        "",
        *_setup_lines(setup_timings),
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


def _make_retrieve(
    requested_modes: list[str],
    text_units: list[TextUnit],
    fusion_depth: int,
    rrf_k: int,
    stack: ExitStack,
) -> tuple[Callable[..., list[SearchResult]], dict[str, float]]:
    retrievers: dict[str, Any] = {}
    setup_timings: dict[str, float] = {}

    if "naive" in requested_modes or "hybrid" in requested_modes:
        started = time.perf_counter()
        retrievers["naive"] = BM25Retriever(text_units)
        setup_timings["bm25_index_build"] = (time.perf_counter() - started) * 1000

    if "vector" in requested_modes or "hybrid" in requested_modes:
        from issue_graphrag.embeddings.sentence_transformer import (
            SentenceTransformerEmbeddingClient,
        )
        from issue_graphrag.storage.qdrant_store import QdrantVectorStore

        settings = load_settings()
        if settings.embedding_provider != "sentence-transformers":
            raise ValueError(
                "Vector evaluation requires EMBEDDING_PROVIDER=sentence-transformers"
            )
        if not settings.vector_db_path.exists():
            raise FileNotFoundError(
                f"Vector index not found at {settings.vector_db_path}; "
                "run scripts/build_vector_index.py first"
            )

        started = time.perf_counter()
        embedding = SentenceTransformerEmbeddingClient(settings.embedding_model)
        setup_timings["embedding_model_load"] = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        store = stack.enter_context(
            QdrantVectorStore(
                path=settings.vector_db_path,
                collection_name=settings.vector_collection,
                vector_size=embedding.dimension,
            )
        )
        setup_timings["qdrant_open"] = (time.perf_counter() - started) * 1000
        if store.count("text_unit") == 0:
            raise ValueError(
                "The Qdrant collection has no TextUnits; rebuild the vector index first"
            )
        retrievers["vector"] = VectorRetriever(embedding, store)

    if "hybrid" in requested_modes:
        retrievers["hybrid"] = HybridRetriever(
            retrievers["naive"],
            retrievers["vector"],
            fusion_depth=fusion_depth,
            rrf_k=rrf_k,
        )

    def retrieve(
        mode: str,
        question: str,
        graph,
        units: list[TextUnit],
        reports: list[CommunityReport],
        top_k: int,
    ) -> list[SearchResult]:
        if mode in retrievers:
            return retrievers[mode].search(question, top_k=top_k)
        if mode == "local":
            return local_search(graph, reports, units, question, top_k=top_k)
        if mode == "global":
            return global_search(reports, question, top_k=top_k)
        raise ValueError(f"Unsupported mode: {mode}")

    return retrieve, setup_timings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval baselines and experiments against annotated evidence."
    )
    parser.add_argument("--queries", default="eval/queries.json")
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=AVAILABLE_MODES,
        default=DEFAULT_MODES,
    )
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
    parser.add_argument("--fusion-depth", type=int, default=DEFAULT_FUSION_DEPTH)
    parser.add_argument("--rrf-k", type=int, default=DEFAULT_RRF_K)
    parser.add_argument("--markdown-output", default="eval/results.md")
    parser.add_argument("--csv-output", default="eval/results.csv")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be at least 1")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.fusion_depth < 1:
        parser.error("--fusion-depth must be at least 1")
    if args.rrf_k < 1:
        parser.error("--rrf-k must be at least 1")

    cases = load_eval_cases(Path(args.queries))
    graph, text_units, reports = _load_processed_data()
    rows: list[EvalRow] = []
    with ExitStack() as stack:
        retrieve, setup_timings = _make_retrieve(
            args.modes,
            text_units,
            fusion_depth=args.fusion_depth,
            rrf_k=args.rrf_k,
            stack=stack,
        )
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
                        retrieve=retrieve,
                    )
                )

    summary = summarize(rows)
    print("\n" + EVAL_NOTE + "\n")
    print("One-time setup timings")
    print("\n".join(_setup_lines(setup_timings)) + "\n")
    print("Summary by mode")
    print("\n".join(_summary_lines(summary)))
    print("\nDetailed results")
    print("\n".join(_detail_lines(rows)))
    _write_markdown(
        Path(args.markdown_output),
        rows,
        summary,
        args.top_k,
        args.repeats,
        setup_timings,
        args.fusion_depth,
        args.rrf_k,
    )
    _write_csv(Path(args.csv_output), rows)
    print(f"\nWrote markdown results to {args.markdown_output}")
    print(f"Wrote CSV results to {args.csv_output}")


if __name__ == "__main__":
    main()
