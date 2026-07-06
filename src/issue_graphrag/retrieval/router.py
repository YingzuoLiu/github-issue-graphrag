from __future__ import annotations

VALID_MODES = {"auto", "naive", "local", "global"}

GLOBAL_HINTS = [
    "overall",
    "overview",
    "main",
    "main theme",
    "themes",
    "trend",
    "roadmap",
    "summary",
    "summarize",
    "contribution areas",
    "contribution opportunities",
    "opportunities",
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
    "component",
    "latency",
    "slow",
    "issue",
    "issue #",
    "pr #",
    "关系",
    "影响",
    "依赖",
    "冲突",
    "模块",
]


def route_query(query: str, requested_mode: str = "auto") -> str:
    """Resolve the retrieval mode for a query.

    Explicit modes are returned unchanged. In auto mode, broad repository-level
    questions go to global search, while specific technical questions default to
    local GraphRAG. The BM25 baseline is only selected explicitly so that auto
    mode continues to exercise the GraphRAG pipeline rather than the baseline.
    """
    if requested_mode not in VALID_MODES:
        raise ValueError(f"Unsupported mode: {requested_mode}")

    if requested_mode != "auto":
        return requested_mode

    lowered = query.lower()
    if any(hint in lowered for hint in GLOBAL_HINTS):
        return "global"

    if "#" in query or any(hint in lowered for hint in LOCAL_HINTS):
        return "local"

    return "local"
