from __future__ import annotations

import networkx as nx

from issue_graphrag.models import CommunityReport, SearchResult, TextUnit


def _score_entity(query: str, name: str, description: str) -> float:
    terms = set(query.lower().replace("_", " ").split())
    text = f"{name} {description}".lower().replace("_", " ")
    return float(sum(1 for term in terms if term in text))


def match_entities(graph: nx.Graph, query: str, top_k: int = 5) -> list[str]:
    scored = []
    for node, data in graph.nodes(data=True):
        score = _score_entity(query, str(node), data.get("description", ""))
        if score > 0:
            scored.append((str(node), score + graph.degree[node] * 0.05))
    return [name for name, _ in sorted(scored, key=lambda item: item[1], reverse=True)[:top_k]]


def build_local_context(
    graph: nx.Graph,
    text_units: list[TextUnit],
    reports: list[CommunityReport],
    query: str,
    top_k_entities: int = 5,
    neighbor_depth: int = 1,
) -> str:
    seed_entities = match_entities(graph, query, top_k=top_k_entities)
    if not seed_entities:
        return ""

    selected_nodes: set[str] = set(seed_entities)
    frontier = set(seed_entities)
    for _ in range(neighbor_depth):
        next_frontier: set[str] = set()
        for node in frontier:
            if graph.has_node(node):
                next_frontier.update(str(n) for n in graph.neighbors(node))
        selected_nodes.update(next_frontier)
        frontier = next_frontier

    selected_edges = []
    source_ids: set[str] = set()
    community_ids: set[str] = set()

    for node in selected_nodes:
        if not graph.has_node(node):
            continue
        node_data = graph.nodes[node]
        source_ids.update(node_data.get("source_ids", []))
        if "community_id" in node_data:
            community_ids.add(str(node_data["community_id"]))

    for source, target, data in graph.edges(data=True):
        if str(source) in selected_nodes and str(target) in selected_nodes:
            selected_edges.append((str(source), str(target), data))
            source_ids.update(data.get("source_ids", []))

    report_by_id = {report.id: report for report in reports}
    related_reports = [report_by_id[cid] for cid in community_ids if cid in report_by_id]
    source_by_id = {unit.id: unit for unit in text_units}
    related_sources = [source_by_id[sid] for sid in source_ids if sid in source_by_id]

    sections: list[str] = []
    sections.append("-----Reports-----")
    for report in related_reports:
        sections.append(f"[{report.id}] {report.title}\n{report.summary}")

    sections.append("\n-----Entities-----")
    for node in sorted(selected_nodes):
        if graph.has_node(node):
            data = graph.nodes[node]
            sections.append(
                f"- {node} | type={data.get('type', 'CONCEPT')} | degree={graph.degree[node]} | "
                f"description={data.get('description', '')}"
            )

    sections.append("\n-----Relationships-----")
    for source, target, data in selected_edges:
        sections.append(
            f"- {source} -- {data.get('relations', [])} -- {target}; "
            f"descriptions={data.get('descriptions', [])}"
        )

    sections.append("\n-----Sources-----")
    for source in related_sources[:8]:
        title = source.metadata.get("document_title", source.document_id)
        sections.append(f"[{source.id}] {title}\n{source.text[:1200]}")

    return "\n".join(sections)


def local_search(
    graph: nx.Graph,
    text_units: list[TextUnit],
    reports: list[CommunityReport],
    query: str,
    top_k_entities: int = 5,
) -> list[SearchResult]:
    context = build_local_context(graph, text_units, reports, query, top_k_entities=top_k_entities)
    if not context:
        return []
    return [SearchResult(id="local_context", score=1.0, text=context)]
