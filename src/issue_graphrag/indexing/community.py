from __future__ import annotations

import networkx as nx


def detect_communities(graph: nx.Graph) -> dict[str, int]:
    """Assign a community id to each node.

    MVP implementation uses NetworkX greedy modularity communities. Later we can
    replace this with Leiden/Louvain for larger graphs.
    """
    if graph.number_of_nodes() == 0:
        return {}

    if graph.number_of_edges() == 0:
        return {node: idx for idx, node in enumerate(graph.nodes)}

    communities = nx.algorithms.community.greedy_modularity_communities(graph, weight="weight")
    assignments: dict[str, int] = {}
    for community_id, nodes in enumerate(communities):
        for node in nodes:
            assignments[str(node)] = community_id
    return assignments


def attach_communities(graph: nx.Graph, assignments: dict[str, int]) -> nx.Graph:
    for node, community_id in assignments.items():
        if graph.has_node(node):
            graph.nodes[node]["community_id"] = community_id
    return graph


def community_subgraphs(graph: nx.Graph) -> dict[int, nx.Graph]:
    groups: dict[int, list[str]] = {}
    for node, data in graph.nodes(data=True):
        community_id = data.get("community_id")
        if community_id is None:
            continue
        groups.setdefault(int(community_id), []).append(str(node))
    return {cid: graph.subgraph(nodes).copy() for cid, nodes in groups.items()}
