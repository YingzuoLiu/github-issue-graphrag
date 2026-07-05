from __future__ import annotations

import json

import networkx as nx

from issue_graphrag.llm.client import LLMClient
from issue_graphrag.models import CommunityReport
from issue_graphrag.prompts import COMMUNITY_REPORT_PROMPT


def _community_payload(graph: nx.Graph) -> str:
    entities = []
    for node, data in graph.nodes(data=True):
        entities.append(
            {
                "name": node,
                "type": data.get("type", "CONCEPT"),
                "description": data.get("description", ""),
            }
        )

    relationships = []
    for source, target, data in graph.edges(data=True):
        relationships.append(
            {
                "source": source,
                "target": target,
                "relations": data.get("relations", []),
                "descriptions": data.get("descriptions", []),
                "weight": data.get("weight", 1.0),
            }
        )

    return json.dumps({"entities": entities, "relationships": relationships}, ensure_ascii=False, indent=2)


def generate_report(community_id: int, graph: nx.Graph, llm: LLMClient) -> CommunityReport:
    payload = _community_payload(graph)
    raw = llm.complete(COMMUNITY_REPORT_PROMPT.format(community_data=payload))

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {
            "title": f"Community {community_id}",
            "summary": raw.strip(),
            "rating": 1.0,
        }

    source_ids: set[str] = set()
    for _, data in graph.nodes(data=True):
        source_ids.update(data.get("source_ids", []))
    for _, _, data in graph.edges(data=True):
        source_ids.update(data.get("source_ids", []))

    return CommunityReport(
        id=str(community_id),
        title=parsed.get("title") or f"Community {community_id}",
        summary=parsed.get("summary") or "",
        rating=float(parsed.get("rating", 1.0)),
        entity_names=[str(n) for n in graph.nodes],
        source_ids=sorted(source_ids),
    )


def generate_reports(communities: dict[int, nx.Graph], llm: LLMClient) -> list[CommunityReport]:
    return [generate_report(cid, subgraph, llm) for cid, subgraph in communities.items()]
