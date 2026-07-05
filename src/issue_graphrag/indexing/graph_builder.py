from __future__ import annotations

import networkx as nx

from issue_graphrag.models import Entity, Relationship


def merge_entity(existing: dict, entity: Entity) -> dict:
    source_ids = sorted(set(existing.get("source_ids", [])) | set(entity.source_ids))
    descriptions = [d for d in [existing.get("description", ""), entity.description] if d]
    return {
        **existing,
        "type": existing.get("type") or entity.type,
        "description": descriptions[0] if descriptions else "",
        "source_ids": source_ids,
    }


def build_graph(entities: list[Entity], relationships: list[Relationship]) -> nx.Graph:
    """Build an undirected entity graph for the MVP.

    Relationship direction is preserved as edge metadata. We use an undirected
    graph for simple community detection and neighbor expansion.
    """
    graph = nx.Graph()

    for entity in entities:
        if graph.has_node(entity.name):
            graph.nodes[entity.name].update(merge_entity(dict(graph.nodes[entity.name]), entity))
        else:
            graph.add_node(
                entity.name,
                type=entity.type,
                description=entity.description,
                source_ids=sorted(set(entity.source_ids)),
            )

    for rel in relationships:
        for name in [rel.source, rel.target]:
            if not graph.has_node(name):
                graph.add_node(name, type="CONCEPT", description="", source_ids=[])

        if graph.has_edge(rel.source, rel.target):
            edge = graph.edges[rel.source, rel.target]
            edge["weight"] = float(edge.get("weight", 1.0)) + rel.weight
            edge["relations"] = sorted(set(edge.get("relations", [])) | {rel.relation})
            edge["descriptions"] = sorted(set(edge.get("descriptions", [])) | {rel.description})
            edge["source_ids"] = sorted(set(edge.get("source_ids", [])) | set(rel.source_ids))
        else:
            graph.add_edge(
                rel.source,
                rel.target,
                source=rel.source,
                target=rel.target,
                relations=[rel.relation],
                descriptions=[rel.description] if rel.description else [],
                weight=rel.weight,
                source_ids=sorted(set(rel.source_ids)),
            )

    return graph


def graph_stats(graph: nx.Graph) -> dict[str, int]:
    return {
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "connected_components": nx.number_connected_components(graph) if graph.number_of_nodes() else 0,
    }
