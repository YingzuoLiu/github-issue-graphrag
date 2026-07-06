from __future__ import annotations

import json
from collections import defaultdict

import networkx as nx

from issue_graphrag.config import load_settings
from issue_graphrag.indexing.report_generator import generate_reports
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient
from issue_graphrag.storage.json_store import read_graph


def make_llm():
    settings = load_settings()

    if settings.llm_provider == "mock":
        return MockLLMClient()

    if settings.llm_provider == "openai-compatible":
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError(
                "LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required for openai-compatible provider."
            )
        return OpenAICompatibleClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")


def main() -> None:
    settings = load_settings()
    processed = settings.processed_data_dir

    graph = read_graph(processed / "graph.json")

    community_nodes = defaultdict(list)
    for node, data in graph.nodes(data=True):
        community_id = data.get("community_id", -1)
        community_nodes[int(community_id)].append(node)

    communities = {
        cid: graph.subgraph(nodes).copy()
        for cid, nodes in community_nodes.items()
        if nodes
    }

    print(f"Regenerating reports for {len(communities)} communities...")

    llm = make_llm()
    reports = generate_reports(communities, llm)
    reports = sorted(
        reports,
        key=lambda report: (report.rating, len(report.entity_names)),
        reverse=True,
    )

    output_path = processed / "community_reports.json"
    with output_path.open("w", encoding="utf-8") as f:
        json.dump([report.model_dump() for report in reports], f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(reports)} reports to {output_path}")


if __name__ == "__main__":
    main()
