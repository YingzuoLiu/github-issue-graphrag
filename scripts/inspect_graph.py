from __future__ import annotations

import argparse
from collections import Counter, defaultdict

import networkx as nx

from issue_graphrag.config import load_settings
from issue_graphrag.models import CommunityReport, TextUnit
from issue_graphrag.storage.json_store import read_graph, read_json


def shorten(text: str, max_len: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the generated GraphRAG graph.")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--entity", type=str, default=None, help="Inspect one entity and its neighbors.")
    args = parser.parse_args()

    settings = load_settings()
    processed = settings.processed_data_dir

    graph = read_graph(processed / "graph.json")
    text_units = [TextUnit.model_validate(x) for x in read_json(processed / "text_units.json")]
    reports = [CommunityReport.model_validate(x) for x in read_json(processed / "community_reports.json")]

    source_by_id = {unit.id: unit for unit in text_units}

    print("\n=== Graph Stats ===")
    print(f"nodes: {graph.number_of_nodes()}")
    print(f"edges: {graph.number_of_edges()}")
    print(f"connected components: {nx.number_connected_components(graph) if graph.number_of_nodes() else 0}")

    print("\n=== Entity Types ===")
    type_counts = Counter(data.get("type", "UNKNOWN") for _, data in graph.nodes(data=True))
    for entity_type, count in type_counts.most_common(args.top_n):
        print(f"{entity_type:24s} {count}")

    print("\n=== Top Entities by Degree ===")
    ranked_nodes = sorted(graph.degree, key=lambda item: item[1], reverse=True)[: args.top_n]
    for node, degree in ranked_nodes:
        data = graph.nodes[node]
        print(f"- {node} | degree={degree} | type={data.get('type')} | {shorten(data.get('description', ''))}")

    print("\n=== Relationship Labels ===")
    rel_counts = Counter()
    for _, _, data in graph.edges(data=True):
        for rel in data.get("relations", []):
            rel_counts[rel] += 1
    for rel, count in rel_counts.most_common(args.top_n):
        print(f"{rel:24s} {count}")

    print("\n=== Largest Connected Components ===")
    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    for idx, component in enumerate(components[:10]):
        sample = sorted(str(n) for n in component)[:8]
        print(f"- component {idx}: size={len(component)} | sample={sample}")

    print("\n=== Communities ===")
    community_nodes = defaultdict(list)
    for node, data in graph.nodes(data=True):
        community_id = data.get("community_id", "NA")
        community_nodes[str(community_id)].append(str(node))

    for community_id, nodes in sorted(community_nodes.items(), key=lambda item: len(item[1]), reverse=True)[:10]:
        print(f"- community {community_id}: size={len(nodes)} | sample={sorted(nodes)[:8]}")

    print("\n=== Community Reports ===")
    for report in reports[:10]:
        print(f"- [{report.id}] {report.title} | rating={report.rating}")
        print(f"  entities={report.entity_names[:8]}")
        print(f"  summary={shorten(report.summary, 240)}")

    if args.entity:
        entity = args.entity
        print(f"\n=== Entity Detail: {entity} ===")
        if not graph.has_node(entity):
            candidates = [n for n in graph.nodes if entity.lower() in str(n).lower()]
            print("Exact entity not found.")
            print(f"Candidates: {candidates[:20]}")
            return

        data = graph.nodes[entity]
        print(f"type: {data.get('type')}")
        print(f"description: {data.get('description')}")
        print(f"source_ids: {data.get('source_ids', [])[:10]}")

        print("\nNeighbors:")
        for nbr in graph.neighbors(entity):
            edge = graph.edges[entity, nbr]
            print(f"- {entity} -- {edge.get('relations', [])} -- {nbr}")
            print(f"  descriptions={edge.get('descriptions', [])[:2]}")

        print("\nSource snippets:")
        for source_id in data.get("source_ids", [])[:5]:
            source = source_by_id.get(source_id)
            if source:
                print(f"- [{source.id}] {source.metadata.get('document_title')}")
                print(f"  {shorten(source.text, 500)}")


if __name__ == "__main__":
    main()
