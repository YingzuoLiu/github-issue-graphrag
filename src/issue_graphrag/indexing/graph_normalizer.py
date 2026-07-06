from __future__ import annotations

import networkx as nx

from issue_graphrag.indexing.normalizer import canonical_entity_name, canonical_relation_label


def _merge_node_data(existing: dict, incoming: dict) -> dict:
    source_ids = sorted(set(existing.get("source_ids", [])) | set(incoming.get("source_ids", [])))

    description = existing.get("description") or incoming.get("description") or ""

    existing_type = existing.get("type", "CONCEPT")
    incoming_type = incoming.get("type", "CONCEPT")
    node_type = incoming_type if existing_type == "CONCEPT" and incoming_type != "CONCEPT" else existing_type

    community_id = existing.get("community_id", incoming.get("community_id"))

    merged = {
        **existing,
        **incoming,
        "type": node_type,
        "description": description,
        "source_ids": source_ids,
    }

    if community_id is not None:
        merged["community_id"] = community_id

    return merged


def normalize_graph(graph: nx.Graph) -> nx.Graph:
    """Canonicalize graph node names and relation labels after graph construction.

    This catches duplicates that slipped through extraction-level normalization,
    such as Graph-RAG / graph_rag / graph-rag or TG / TrustGraph.
    """
    normalized = nx.Graph()

    node_name_map: dict[str, str] = {}

    for node, data in graph.nodes(data=True):
        canonical = canonical_entity_name(str(node), data.get("type"))
        node_name_map[str(node)] = canonical

        if normalized.has_node(canonical):
            merged = _merge_node_data(dict(normalized.nodes[canonical]), dict(data))
            normalized.nodes[canonical].update(merged)
        else:
            normalized.add_node(canonical, **dict(data))

    for source, target, data in graph.edges(data=True):
        new_source = node_name_map.get(str(source), canonical_entity_name(str(source)))
        new_target = node_name_map.get(str(target), canonical_entity_name(str(target)))

        if not new_source or not new_target or new_source == new_target:
            continue

        relations = [canonical_relation_label(r) for r in data.get("relations", [])]
        descriptions = data.get("descriptions", [])
        source_ids = data.get("source_ids", [])
        weight = float(data.get("weight", 1.0))

        if normalized.has_edge(new_source, new_target):
            edge = normalized.edges[new_source, new_target]
            edge["relations"] = sorted(set(edge.get("relations", [])) | set(relations))
            edge["descriptions"] = sorted(set(edge.get("descriptions", [])) | set(descriptions))
            edge["source_ids"] = sorted(set(edge.get("source_ids", [])) | set(source_ids))
            edge["weight"] = float(edge.get("weight", 1.0)) + weight
        else:
            normalized.add_edge(
                new_source,
                new_target,
                relations=sorted(set(relations)),
                descriptions=sorted(set(descriptions)),
                source_ids=sorted(set(source_ids)),
                weight=weight,
            )

    return normalized
