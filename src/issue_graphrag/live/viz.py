"""Graphviz DOT rendering for the affected-subgraph view.

Rendered client-side by ``st.graphviz_chart``, so the demo shows a focused,
colour-coded neighbourhood without pulling in another visualisation dependency
or dumping a 200-node hairball on the page.
"""

from __future__ import annotations

import networkx as nx

CHANGE_STYLE = {
    "added": {"color": "#2f9e44", "penwidth": "2.2", "style": "filled", "fill": "#d3f9d8"},
    "changed": {"color": "#e8590c", "penwidth": "2.0", "style": "filled", "fill": "#ffe8cc"},
    "invalidated": {"color": "#868e96", "penwidth": "1.4", "style": "filled,dashed", "fill": "#f1f3f5"},
    "unchanged": {"color": "#495057", "penwidth": "1.0", "style": "filled", "fill": "#ffffff"},
}

NODE_SHAPE = {
    "ISSUE": "box",
    "PULL_REQUEST": "component",
    "FILE": "note",
    "MODULE": "folder",
}


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _node_label(name: str, data: dict) -> str:
    node_type = data.get("type", "CONCEPT")
    state = data.get("state")
    suffix = f"\\n({state})" if state else ""
    if node_type in ("ISSUE", "PULL_REQUEST"):
        title = str(data.get("description") or "")
        if len(title) > 34:
            title = title[:31] + "..."
        return f"{name}{suffix}\\n{_escape(title)}"
    return f"{name}{suffix}"


def subgraph_dot(graph: nx.Graph, title: str | None = None) -> str:
    """Render an annotated subgraph as DOT source."""
    lines = ["digraph contribution {", "  rankdir=LR;", "  bgcolor=\"transparent\";"]
    if title:
        lines.append(f'  label="{_escape(title)}"; labelloc=t; fontsize=11;')
    lines.append('  node [fontname="Helvetica" fontsize=10 fontcolor="#212529"];')
    lines.append('  edge [fontname="Helvetica" fontsize=9 fontcolor="#495057"];')

    for name, data in sorted(graph.nodes(data=True)):
        style = CHANGE_STYLE.get(data.get("change", "unchanged"), CHANGE_STYLE["unchanged"])
        shape = NODE_SHAPE.get(data.get("type", "CONCEPT"), "ellipse")
        lines.append(
            f'  "{_escape(name)}" [label="{_node_label(str(name), data)}" shape={shape} '
            f'style="{style["style"]}" fillcolor="{style["fill"]}" '
            f'color="{style["color"]}" penwidth={style["penwidth"]}];'
        )

    for source, target, data in sorted(graph.edges(data=True), key=lambda e: (str(e[0]), str(e[1]))):
        style = CHANGE_STYLE.get(data.get("change", "unchanged"), CHANGE_STYLE["unchanged"])
        directed = data.get("directed_relations") or []
        inferred_only = data.get("origins") == ["llm"]
        for row in directed or [{"source": source, "target": target, "relation": "related_to"}]:
            lines.append(
                f'  "{_escape(row["source"])}" -> "{_escape(row["target"])}" '
                f'[label="{_escape(row["relation"])}" color="{style["color"]}" '
                f'penwidth={style["penwidth"]} '
                f'style="{"dashed" if inferred_only else "solid"}"];'
            )

    lines.append("}")
    return "\n".join(lines)


def legend_markdown() -> str:
    return (
        "**green** added by this event  ·  **orange** state changed  ·  "
        "**grey dashed** invalidated  ·  solid edges are stated by GitHub, "
        "dashed edges are inferred"
    )
