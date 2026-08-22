"""Scoped community report regeneration.

Report generation is the other expensive LLM call in the pipeline, so it gets
the same treatment as extraction: communities whose membership did not change
keep the report they already have.
"""

from __future__ import annotations

import hashlib
import json

import networkx as nx

from issue_graphrag.indexing.community import (
    attach_communities,
    community_subgraphs,
    detect_communities,
)
from issue_graphrag.indexing.report_generator import generate_report
from issue_graphrag.llm.client import LLMClient
from issue_graphrag.models import CommunityReport


def report_fingerprint(subgraph: nx.Graph) -> str:
    """Hash of everything the report prompt is built from.

    Membership alone is not enough. A community can keep exactly the same nodes
    while the relationships between them change, and a report that still
    describes the old relationships is stale even though nobody joined or left.
    """
    payload = {
        "entities": sorted(
            (str(name), str(data.get("type", "")), str(data.get("description", "")))
            for name, data in subgraph.nodes(data=True)
        ),
        "relationships": sorted(
            (
                str(source),
                str(target),
                tuple(sorted(data.get("relations", []))),
                tuple(sorted(data.get("descriptions", []))),
                round(float(data.get("weight", 1.0)), 6),
            )
            for source, target, data in subgraph.edges(data=True)
        ),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=list)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def refresh_reports(
    graph: nx.Graph,
    previous: list[CommunityReport],
    llm: LLMClient,
) -> tuple[list[CommunityReport], list[str]]:
    """Return reports for the current graph plus the ids that were regenerated.

    Communities are matched by report fingerprint rather than by id, because
    greedy modularity renumbers communities whenever the graph changes shape.
    """
    graph = attach_communities(graph, detect_communities(graph))
    reusable = {
        report.metadata.get("fingerprint"): report
        for report in previous
        if report.metadata.get("fingerprint")
    }

    reports: list[CommunityReport] = []
    regenerated: list[str] = []

    for community_id, subgraph in sorted(community_subgraphs(graph).items()):
        fingerprint = report_fingerprint(subgraph)
        cached = reusable.get(fingerprint)
        if cached is not None:
            reports.append(cached.model_copy(update={"id": str(community_id)}))
            continue

        report = generate_report(community_id, subgraph, llm)
        report.metadata = {**report.metadata, "fingerprint": fingerprint}
        reports.append(report)
        regenerated.append(str(community_id))

    return reports, regenerated
