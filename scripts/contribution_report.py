"""Query the live contribution graph: what to work on, and why that changed.

Every answer is traceable. --explain prints the evidence behind each edge and
--history prints when a fact started and stopped being true, including facts
that have been invalidated.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.models import LiveState
from issue_graphrag.live.projection import project_graph
from issue_graphrag.live.store import read_state


def resolve_moment(state: LiveState, as_of: str | None) -> str | None:
    return as_of or None


def print_ranking(state: LiveState, moment: str | None, top: int, include_closed: bool) -> None:
    graph = project_graph(state, moment)
    ranked = opportunities(graph, include_closed=include_closed)[:top]

    label = f"as of {moment}" if moment else "now"
    print(f"Contribution opportunities ({label})")
    print("=" * (28 + len(label)))
    if not ranked:
        print("  (none)")
        return

    for item in ranked:
        print(f"\n  {item.score:5.2f}  {item.status:<10} {item.node}  {item.title}")
        if item.url:
            print(f"         {item.url}")
        for reason in item.reasons:
            print(f"         - {reason}")
        for evidence in item.evidence[1:]:
            suffix = f" ({evidence.url})" if evidence.url else ""
            print(f"         evidence: {evidence.label}{suffix}")


def print_explain(state: LiveState, node: str, moment: str | None) -> None:
    graph = project_graph(state, moment)
    if not graph.has_node(node):
        raise SystemExit(f"'{node}' is not in the graph. Try --list-nodes.")

    data = graph.nodes[node]
    print(f"{node}")
    print("=" * len(node))
    print(f"  type        : {data.get('type')}")
    print(f"  state       : {data.get('state')}")
    print(f"  labels      : {', '.join(data.get('labels', [])) or '(none)'}")
    print(f"  url         : {data.get('url') or '(none)'}")
    print(f"  first seen  : {data.get('first_seen')}")
    print(f"  description : {data.get('description') or '(none)'}")

    print("\n  Edges")
    for neighbour in sorted(graph.neighbors(node)):
        edge = graph.edges[node, neighbour]
        for row in edge.get("directed_relations", []):
            marker = "github" if row["origin"] == "github" else "inferred"
            print(f"    [{marker:8}] {row['source']} --{row['relation']}--> {row['target']}")
        for evidence in edge.get("evidence", [])[:3]:
            suffix = f" {evidence['url']}" if evidence.get("url") else ""
            print(f"        via {evidence['kind']} {evidence['ref']}{suffix}")
            if evidence.get("snippet"):
                print(f'        "{evidence["snippet"][:150]}"')


def print_history(state: LiveState, node: str) -> None:
    facts = [f for f in state.facts if f.subject == node or f.object == node]
    facts.sort(key=lambda f: (f.valid_from, f.key))

    print(f"History for {node}")
    print("=" * (12 + len(node)))
    if not facts:
        print("  (no facts)")
        return

    for fact in facts:
        window = f"{fact.valid_from} -> {fact.valid_to or 'now'}"
        status = "invalidated" if fact.valid_to else "valid"
        print(f"  [{status:<11}] {window}  [{fact.origin}] {fact.label()}")
        if fact.invalidated_by:
            print(f"                  invalidated by delivery {fact.invalidated_by}")
        for evidence in fact.evidence[:2]:
            suffix = f" {evidence.url}" if evidence.url else ""
            print(f"                  via {evidence.kind} {evidence.ref}{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=None, help="path to live_state.json")
    parser.add_argument("--as-of", default=None, help="project the graph at an ISO timestamp")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--include-closed", action="store_true")
    parser.add_argument("--explain", default=None, help="show one node with its evidence")
    parser.add_argument("--history", default=None, help="show every fact ever asserted about a node")
    parser.add_argument("--list-nodes", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit the ranking as JSON")
    args = parser.parse_args()

    settings = load_settings()
    state_path = args.state or settings.processed_data_dir / "live_state.json"
    if not state_path.exists():
        raise SystemExit(f"{state_path} not found. Run scripts/replay_events.py first.")

    state = read_state(state_path)
    moment = resolve_moment(state, args.as_of)

    if args.list_nodes:
        graph = project_graph(state, moment)
        for name, data in sorted(graph.nodes(data=True)):
            print(f"  {data.get('type', 'CONCEPT'):<14} {name}")
        return

    if args.history:
        print_history(state, args.history)
        return

    if args.explain:
        print_explain(state, args.explain, moment)
        return

    if args.json:
        graph = project_graph(state, moment)
        ranked = opportunities(graph, include_closed=args.include_closed)[: args.top]
        print(json.dumps([item.model_dump() for item in ranked], indent=2, ensure_ascii=False))
        return

    print_ranking(state, moment, args.top, args.include_closed)


if __name__ == "__main__":
    main()
