"""Scoped community report regeneration.

Report generation is the other expensive LLM call in the pipeline, so it gets
the same treatment as extraction: communities whose membership did not change
keep the report they already have.
"""

from __future__ import annotations

import networkx as nx

from issue_graphrag.indexing.community import (
    attach_communities,
    community_subgraphs,
    detect_communities,
)
from issue_graphrag.indexing.report_generator import generate_report
from issue_graphrag.llm.client import LLMClient
from issue_graphrag.models import CommunityReport


def refresh_reports(
    graph: nx.Graph,
    previous: list[CommunityReport],
    llm: LLMClient,
) -> tuple[list[CommunityReport], list[str]]:
    """Return reports for the current graph plus the ids that were regenerated.

    Communities are matched by membership rather than by id, because greedy
    modularity renumbers communities whenever the graph changes shape.
    """
    graph = attach_communities(graph, detect_communities(graph))
    reusable = {frozenset(report.entity_names): report for report in previous}

    reports: list[CommunityReport] = []
    regenerated: list[str] = []

    for community_id, subgraph in sorted(community_subgraphs(graph).items()):
        members = frozenset(str(node) for node in subgraph.nodes)
        cached = reusable.get(members)
        if cached is not None:
            reports.append(cached.model_copy(update={"id": str(community_id)}))
            continue

        reports.append(generate_report(community_id, subgraph, llm))
        regenerated.append(str(community_id))

    return reports, regenerated
