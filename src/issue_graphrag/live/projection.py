from __future__ import annotations

import re
from typing import Any, Iterable

import networkx as nx

from issue_graphrag.live.documents import text_unit_ids_by_document
from issue_graphrag.live.facts import ItemIndex
from issue_graphrag.live.models import Fact, GraphDelta, LiveState
from issue_graphrag.live.ontology import permits

_NUMBER = re.compile(r"#(\d+)$")

#: Node types GitHub states outright. They win over any LLM-assigned type.
_GITHUB_TYPES = ("ISSUE", "PULL_REQUEST", "FILE", "MODULE")


def _edge_key(subject: str, obj: str) -> tuple[str, str]:
    return (subject, obj) if subject <= obj else (obj, subject)


def _source_ids(fact: Fact, units_by_document: dict[str, list[str]]) -> list[str]:
    """Ground a fact in TextUnits so live graph edges stay usable by retrieval."""
    ids = {e.text_unit_id for e in fact.evidence if e.text_unit_id}
    if fact.origin == "github":
        ids.update(units_by_document.get(fact.document_id, []))
    return sorted(ids)


def _better_type(current: str, candidate: str, candidate_origin: str) -> str:
    if candidate_origin == "github" and candidate in _GITHUB_TYPES:
        return candidate
    if current in _GITHUB_TYPES:
        return current
    if current in ("", "CONCEPT") and candidate:
        return candidate
    return current


def project_graph(
    state: LiveState,
    moment: str | None = None,
    facts: Iterable[Fact] | None = None,
) -> nx.Graph:
    """Fold versioned facts into the entity graph as it stood at ``moment``.

    Two passes: node facts first, so relation facts can be checked against the
    ontology using types that are actually known, then relation facts. The whole
    function is a pure fold over the fact set, which is what makes an
    incremental replay and a full rebuild converge on the same graph: only the
    expensive extraction step is scoped to affected documents, never this.
    """
    selected = list(facts) if facts is not None else state.valid_facts(moment)
    selected.sort(key=lambda fact: fact.key)

    # Extraction cannot know that "#950" is a pull request until the live index
    # has seen it, so the rename is applied here, where it is a pure function of
    # the current records, instead of being baked into stored facts.
    rename = ItemIndex(state.items).rename_map()
    units_by_document = text_unit_ids_by_document(state)
    graph = nx.Graph()

    def ensure_node(name: str) -> dict[str, Any]:
        if not graph.has_node(name):
            match = _NUMBER.search(name)
            graph.add_node(
                name,
                type="CONCEPT",
                description="",
                state=None,
                labels=[],
                url=None,
                number=int(match.group(1)) if match else None,
                source_ids=[],
                origins=[],
                first_seen=None,
                last_seen=None,
            )
        return graph.nodes[name]

    def touch(name: str, fact: Fact) -> dict[str, Any]:
        node = ensure_node(name)
        node["origins"] = sorted(set(node["origins"]) | {fact.origin})
        node["first_seen"] = min(filter(None, [node["first_seen"], fact.valid_from]))
        node["last_seen"] = max(filter(None, [node["last_seen"], fact.observed_at]))
        node["source_ids"] = sorted(
            set(node["source_ids"]) | set(_source_ids(fact, units_by_document))
        )
        return node

    entity_facts = [fact for fact in selected if fact.kind == "entity"]
    relation_facts = [fact for fact in selected if fact.kind == "relation"]

    for fact in entity_facts:
        subject = rename.get(fact.subject, fact.subject)
        node = touch(subject, fact)

        if fact.predicate == "is_a":
            node["type"] = _better_type(node["type"], fact.object, fact.origin)
            if fact.description and (not node["description"] or fact.origin == "github"):
                node["description"] = fact.description
            for item in fact.evidence:
                if item.url and not node["url"]:
                    node["url"] = item.url
        elif fact.predicate == "has_state":
            node["state"] = fact.object
        elif fact.predicate == "has_label":
            node["labels"] = sorted(set(node["labels"]) | {fact.object})

    for fact in relation_facts:
        subject = rename.get(fact.subject, fact.subject)
        obj = rename.get(fact.object, fact.object)
        if subject == obj:
            continue

        subject_type = graph.nodes[subject]["type"] if graph.has_node(subject) else None
        object_type = graph.nodes[obj]["type"] if graph.has_node(obj) else None
        if fact.origin == "llm" and not permits(fact.predicate, subject_type, object_type):
            continue

        touch(subject, fact)
        touch(obj, fact)
        source, target = _edge_key(subject, obj)

        if not graph.has_edge(source, target):
            graph.add_edge(
                source,
                target,
                relations=[],
                directed_relations=[],
                descriptions=[],
                source_ids=[],
                origins=[],
                github_relations=[],
                llm_relations=[],
                evidence=[],
                weight=0.0,
                first_seen=fact.valid_from,
                last_seen=fact.observed_at,
            )

        edge = graph.edges[source, target]
        edge["relations"] = sorted(set(edge["relations"]) | {fact.predicate})
        edge["origins"] = sorted(set(edge["origins"]) | {fact.origin})
        bucket = "github_relations" if fact.origin == "github" else "llm_relations"
        edge[bucket] = sorted(set(edge[bucket]) | {fact.predicate})
        if fact.description:
            edge["descriptions"] = sorted(set(edge["descriptions"]) | {fact.description})
        edge["source_ids"] = sorted(
            set(edge["source_ids"]) | set(_source_ids(fact, units_by_document))
        )
        edge["weight"] = round(edge["weight"] + fact.weight, 6)
        edge["first_seen"] = min(edge["first_seen"], fact.valid_from)
        edge["last_seen"] = max(edge["last_seen"], fact.observed_at)
        edge["directed_relations"].append(
            {
                "source": subject,
                "target": obj,
                "relation": fact.predicate,
                "origin": fact.origin,
                "document_id": fact.document_id,
            }
        )
        edge["evidence"].extend(
            {
                "relation": fact.predicate,
                "origin": fact.origin,
                "kind": item.kind,
                "ref": item.ref,
                "url": item.url,
                "snippet": item.snippet,
            }
            for item in fact.evidence
        )

    for _, _, data in graph.edges(data=True):
        data["directed_relations"].sort(
            key=lambda row: (row["source"], row["relation"], row["target"])
        )

    return graph


def graph_signature(graph: nx.Graph) -> dict[str, list]:
    """Order-independent structural fingerprint used for consistency checks.

    Timestamps are deliberately excluded: a rebuild observes everything at once,
    while an incremental replay remembers when each fact first appeared.
    """
    nodes = sorted(
        (
            str(name),
            str(data.get("type", "")),
            str(data.get("state") or ""),
            tuple(sorted(data.get("labels", []))),
            str(data.get("description", "")),
            tuple(sorted(data.get("source_ids", []))),
        )
        for name, data in graph.nodes(data=True)
    )
    edges = sorted(
        (
            str(source),
            str(target),
            tuple(sorted(data.get("relations", []))),
            tuple(sorted(data.get("origins", []))),
            tuple(sorted(data.get("descriptions", []))),
            tuple(sorted(data.get("source_ids", []))),
            round(float(data.get("weight", 0.0)), 6),
        )
        for source, target, data in graph.edges(data=True)
    )
    return {"nodes": [list(row) for row in nodes], "edges": [list(row) for row in edges]}


def diff_graphs(before: nx.Graph, after: nx.Graph) -> dict[str, list]:
    """Node and edge level difference between two projections."""
    before_nodes, after_nodes = set(before.nodes), set(after.nodes)
    before_edges = {_edge_key(str(u), str(v)) for u, v in before.edges}
    after_edges = {_edge_key(str(u), str(v)) for u, v in after.edges}

    changed: list[str] = []
    for name in sorted(before_nodes & after_nodes):
        old, new = before.nodes[name], after.nodes[name]
        if (old.get("state"), sorted(old.get("labels", []))) != (
            new.get("state"),
            sorted(new.get("labels", [])),
        ):
            changed.append(str(name))

    return {
        "added_nodes": sorted(str(n) for n in after_nodes - before_nodes),
        "removed_nodes": sorted(str(n) for n in before_nodes - after_nodes),
        "added_edges": sorted(after_edges - before_edges),
        "removed_edges": sorted(before_edges - after_edges),
        "changed_nodes": changed,
    }


def event_subgraph(
    state: LiveState,
    delta: GraphDelta,
    hops: int = 1,
    moment: str | None = None,
) -> nx.Graph:
    """The 1-2 hop neighbourhood one event touched, annotated for display.

    Invalidated facts are re-added as ghost elements so the view can show what
    stopped being true instead of silently dropping it.
    """
    invalidated = [item.fact for item in delta.fact_changes if item.change == "invalidated"]
    graph = project_graph(state, moment)
    ghost = project_graph(state, moment, facts=invalidated)

    for name, data in ghost.nodes(data=True):
        if not graph.has_node(name):
            graph.add_node(name, **data)
            graph.nodes[name]["change"] = "invalidated"
    for source, target, data in ghost.edges(data=True):
        if not graph.has_edge(source, target):
            graph.add_edge(source, target, **data)
            graph.edges[source, target]["change"] = "invalidated"

    added_edges = {_edge_key(u, v) for u, v in delta.added_edges}
    removed_edges = {_edge_key(u, v) for u, v in delta.removed_edges}
    added_nodes, changed_nodes = set(delta.added_nodes), set(delta.changed_nodes)

    for name, data in graph.nodes(data=True):
        if data.get("change"):
            continue
        if name in added_nodes:
            data["change"] = "added"
        elif name in changed_nodes:
            data["change"] = "changed"
        else:
            data["change"] = "unchanged"

    for source, target, data in graph.edges(data=True):
        if data.get("change"):
            continue
        key = _edge_key(str(source), str(target))
        if key in added_edges:
            data["change"] = "added"
        elif key in removed_edges:
            data["change"] = "invalidated"
        else:
            data["change"] = "unchanged"

    rename = ItemIndex(state.items).rename_map()
    focus = {
        rename.get(name, name)
        for name in delta.focus_nodes()
        if graph.has_node(rename.get(name, name))
    }
    if not focus:
        return graph

    selected = set(focus)
    frontier = set(focus)
    for _ in range(max(hops, 0)):
        neighbours: set[str] = set()
        for name in frontier:
            neighbours.update(str(n) for n in graph.neighbors(name))
        frontier = neighbours - selected
        selected |= neighbours

    return graph.subgraph(selected).copy()
