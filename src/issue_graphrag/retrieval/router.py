from __future__ import annotations


GLOBAL_HINTS = [
    "overall",
    "main theme",
    "themes",
    "trend",
    "roadmap",
    "summary",
    "summarize",
    "contribution areas",
    "主要方向",
    "整体",
    "总结",
    "趋势",
    "共性",
    "值得贡献",
]

LOCAL_HINTS = [
    "affect",
    "impact",
    "related",
    "relationship",
    "depends",
    "conflict",
    "module",
    "issue #",
    "pr #",
    "关系",
    "影响",
    "依赖",
    "冲突",
    "模块",
]


def route_query(query: str) -> str:
    lowered = query.lower()
    if any(hint in lowered for hint in GLOBAL_HINTS):
        return "global"
    if "#" in query or any(hint in lowered for hint in LOCAL_HINTS):
        return "local"
    return "naive"
