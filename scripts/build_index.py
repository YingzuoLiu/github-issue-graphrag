from __future__ import annotations

import argparse

from issue_graphrag.chunker import documents_to_text_units
from issue_graphrag.config import load_settings
from issue_graphrag.indexing.community import attach_communities, community_subgraphs, detect_communities
from issue_graphrag.indexing.extractor import extract_all
from issue_graphrag.indexing.graph_builder import build_graph, graph_stats
from issue_graphrag.indexing.report_generator import generate_reports
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient
from issue_graphrag.models import SourceDocument
from issue_graphrag.storage.json_store import read_json, write_graph, write_json, write_models


def make_llm():
    settings = load_settings()
    if settings.llm_provider == "openai-compatible":
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError("LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required")
        return OpenAICompatibleClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
    return MockLLMClient()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a lightweight GraphRAG index from source documents.")
    parser.add_argument("input", help="Path to raw SourceDocument JSON list")
    args = parser.parse_args()

    settings = load_settings()
    llm = make_llm()

    raw_docs = read_json(settings.raw_data_dir / args.input if not args.input.startswith("/") else args.input)
    documents = [SourceDocument.model_validate(item) for item in raw_docs]

    text_units = documents_to_text_units(documents)
    extraction = extract_all(text_units, llm)
    graph = build_graph(extraction.entities, extraction.relationships)
    assignments = detect_communities(graph)
    graph = attach_communities(graph, assignments)
    reports = generate_reports(community_subgraphs(graph), llm)

    output_dir = settings.processed_data_dir
    write_models(output_dir / "text_units.json", text_units)
    write_json(output_dir / "entities.json", [entity.model_dump() for entity in extraction.entities])
    write_json(output_dir / "relationships.json", [rel.model_dump() for rel in extraction.relationships])
    write_graph(output_dir / "graph.json", graph)
    write_models(output_dir / "community_reports.json", reports)
    write_json(output_dir / "stats.json", graph_stats(graph))

    print(f"Built index in {output_dir}")
    print(graph_stats(graph))


if __name__ == "__main__":
    main()
