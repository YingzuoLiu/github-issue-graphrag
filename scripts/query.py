from __future__ import annotations

import argparse

from issue_graphrag.config import load_settings
from issue_graphrag.models import CommunityReport, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import NaiveBM25Search
from issue_graphrag.retrieval.router import route_query
from issue_graphrag.storage.json_store import read_graph, read_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the local GraphRAG index.")
    parser.add_argument("question")
    parser.add_argument("--mode", choices=["auto", "naive", "local", "global"], default="auto")
    args = parser.parse_args()

    settings = load_settings()
    processed = settings.processed_data_dir

    text_units = [TextUnit.model_validate(item) for item in read_json(processed / "text_units.json")]
    reports = [CommunityReport.model_validate(item) for item in read_json(processed / "community_reports.json")]
    graph = read_graph(processed / "graph.json")

    mode = route_query(args.question) if args.mode == "auto" else args.mode
    print(f"Mode: {mode}\n")

    if mode == "naive":
        results = NaiveBM25Search(text_units).search(args.question, top_k=5)
    elif mode == "local":
        results = local_search(graph, text_units, reports, args.question, top_k_entities=5)
    else:
        results = global_search(reports, args.question, top_k=8)

    if not results:
        print("No results found.")
        return

    for result in results:
        print(f"### {result.id} | score={result.score:.3f}\n")
        print(result.text)
        print("\n---\n")


if __name__ == "__main__":
    main()
