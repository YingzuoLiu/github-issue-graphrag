from __future__ import annotations

import json

from issue_graphrag.llm.client import LLMClient
from issue_graphrag.models import CommunityReport, SearchResult
from issue_graphrag.prompts import GLOBAL_MAP_PROMPT, GLOBAL_REDUCE_PROMPT


def _report_text(report: CommunityReport) -> str:
    return f"[{report.id}] {report.title}\nRating: {report.rating}\nEntities: {', '.join(report.entity_names[:20])}\n{report.summary}"


def global_search_context(reports: list[CommunityReport], top_k: int = 8) -> str:
    selected = sorted(reports, key=lambda r: r.rating, reverse=True)[:top_k]
    return "\n\n".join(_report_text(report) for report in selected)


def global_map_reduce(query: str, reports: list[CommunityReport], llm: LLMClient, top_k: int = 8) -> str:
    selected_context = global_search_context(reports, top_k=top_k)
    if not selected_context:
        return "No community reports are available."

    raw_points = llm.complete(GLOBAL_MAP_PROMPT.format(question=query, reports=selected_context))
    try:
        points = json.loads(raw_points).get("points", [])
    except json.JSONDecodeError:
        points = [{"description": raw_points, "score": 1}]

    useful_points = [p for p in points if p.get("score", 0) > 0 and p.get("description")]
    useful_points = sorted(useful_points, key=lambda p: p.get("score", 0), reverse=True)

    point_text = "\n".join(
        f"- score={p.get('score', 0)}: {p.get('description', '')}" for p in useful_points
    )
    if not point_text:
        point_text = selected_context

    return llm.complete(GLOBAL_REDUCE_PROMPT.format(question=query, points=point_text))


def global_search(reports: list[CommunityReport], query: str, top_k: int = 8) -> list[SearchResult]:
    context = global_search_context(reports, top_k=top_k)
    if not context:
        return []
    return [SearchResult(id="global_context", score=1.0, text=context)]
