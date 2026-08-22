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
    "CONTRIBUTOR": "oval",
}


def _escape(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def _node_label(name: str, data: dict) -> str:
    node_type = data.get("type", "CONCEPT")
    state = data.get("state")
    suffix = f"\\n({state})" if state else ""
    if node_type == "FILE":
        # Files are identified by full path but shown by basename, with the
        # directory kept as a second line so same-named files stay tellable apart.
        head, _, tail = name.rpartition("/")
        return f"{_escape(tail)}\\n{_escape(head)}" if head else _escape(tail)
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
        # One row per asserting fact, so the same triple appears once per document
        # that stated it. That distinction matters to the fingerprint, but drawing
        # it would stack identical parallel arrows on top of each other. Origin is
        # deliberately not part of the identity here: every arrow attribute below
        # comes from the row's triple or from edge-level data, so two rows that
        # differ only by origin would render as two identical arrows.
        directed = distinct_relations(data.get("directed_relations") or [])
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


def distinct_relations(rows: list[dict], *, include_origin: bool = False) -> list[dict]:
    """Collapse projection rows that would render identically.

    ``project_graph`` stores one ``directed_relations`` row per asserting fact,
    because ``graph_signature`` fingerprints the asserting document. A renderer
    that copies that row-per-fact shape shows the same triple more than once.

    Whether ``origin`` belongs in the identity depends on the renderer, so the
    caller states it rather than the helper guessing:

    - The DOT view draws solid-vs-dashed from edge-level ``origins``, so nothing
      in an arrow varies with the row's origin. ``include_origin=False`` there.
    - ``contribution_report.py --explain`` prints a per-row ``[github]`` or
      ``[inferred]`` marker, so dropping origin from the identity would hide a
      GitHub assertion behind an inferred one. ``include_origin=True`` there.
    """
    seen: set[tuple[str, ...]] = set()
    unique: list[dict] = []
    for row in rows:
        key = (str(row["source"]), str(row["relation"]), str(row["target"]))
        if include_origin:
            key += (str(row["origin"]),)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def legend_markdown() -> str:
    return (
        "**green** added by this event  ·  **orange** state changed  ·  "
        "**grey dashed** invalidated  ·  solid edges are stated by GitHub, "
        "dashed edges are inferred"
    )
