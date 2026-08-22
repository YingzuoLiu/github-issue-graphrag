"""Rendering-layer regression tests.

``project_graph`` keeps one ``directed_relations`` row per asserting fact so
``graph_signature`` can fingerprint provenance. Renderers that copy that shape
verbatim draw the same triple twice; these tests pin the collapse and, just as
importantly, pin what must *not* collapse.
"""

from __future__ import annotations

import networkx as nx

from issue_graphrag.live.viz import distinct_relations, subgraph_dot


def relation(source: str, target: str, predicate: str, origin: str, document_id: str) -> dict:
    return {
        "source": source,
        "target": target,
        "relation": predicate,
        "origin": origin,
        "document_id": document_id,
    }


def edge_lines(dot: str) -> list[str]:
    return [line.strip() for line in dot.splitlines() if "->" in line]


def one_edge_graph(rows: list[dict], origins: list[str]) -> nx.Graph:
    graph = nx.Graph()
    graph.add_node("Issue #944", type="ISSUE", change="unchanged")
    graph.add_node("kafka_backend.py", type="FILE", change="unchanged")
    graph.add_edge(
        "Issue #944",
        "kafka_backend.py",
        directed_relations=rows,
        origins=origins,
        change="unchanged",
    )
    return graph


def test_a_triple_asserted_by_two_documents_is_drawn_once():
    rows = [
        relation("Issue #944", "kafka_backend.py", "mentions", "llm", "issue-944"),
        relation("Issue #944", "kafka_backend.py", "mentions", "llm", "pull-950"),
    ]
    lines = edge_lines(subgraph_dot(one_edge_graph(rows, ["llm"]), title="t"))

    assert len(lines) == 1
    assert len(set(lines)) == 1


def test_a_triple_asserted_by_two_documents_is_explained_once():
    rows = [
        relation("Issue #944", "kafka_backend.py", "mentions", "llm", "issue-944"),
        relation("Issue #944", "kafka_backend.py", "mentions", "llm", "pull-950"),
    ]

    assert len(distinct_relations(rows, include_origin=True)) == 1
    assert len(distinct_relations(rows, include_origin=False)) == 1


def test_direction_relation_and_endpoints_are_never_merged():
    rows = [
        relation("Issue #944", "kafka_backend.py", "mentions", "llm", "d1"),
        # reversed direction
        relation("kafka_backend.py", "Issue #944", "mentions", "llm", "d1"),
        # different predicate
        relation("Issue #944", "kafka_backend.py", "touches", "llm", "d1"),
    ]

    assert len(distinct_relations(rows, include_origin=False)) == 3
    assert len(distinct_relations(rows, include_origin=True)) == 3
    assert len(edge_lines(subgraph_dot(one_edge_graph(rows, ["llm"]), title="t"))) == 3


def test_origin_is_part_of_the_identity_only_where_the_renderer_shows_it():
    """The two renderers disagree on purpose, so the disagreement is pinned here.

    ``--explain`` prints a per-row ``[github]`` / ``[inferred]`` marker, so a
    GitHub assertion must not hide behind an inferred one. The DOT view takes
    solid-vs-dashed from edge-level ``origins``, so keeping origin in the
    identity there would emit two arrows that are identical in every attribute.
    """
    rows = [
        relation("Issue #944", "kafka_backend.py", "mentions", "github", "issue-944"),
        relation("Issue #944", "kafka_backend.py", "mentions", "llm", "issue-944"),
    ]

    assert len(distinct_relations(rows, include_origin=True)) == 2
    assert len(distinct_relations(rows, include_origin=False)) == 1

    lines = edge_lines(subgraph_dot(one_edge_graph(rows, ["github", "llm"]), title="t"))
    assert len(lines) == 1


def test_an_edge_with_no_directed_relations_still_draws_one_arrow():
    graph = one_edge_graph([], ["llm"])
    assert len(edge_lines(subgraph_dot(graph, title="t"))) == 1


def test_the_replayed_demo_graph_draws_no_duplicate_arrows(seeded_state, demo_events, extractor):
    """The regression as it actually appeared: 3 of 24 relations in the fixture.

    Issue #944's own body and PR #950 both mention ``kafka_backend.py``, so the
    projection legitimately holds two rows for one triple.
    """
    from issue_graphrag.live.indexer import replay
    from issue_graphrag.live.projection import project_graph

    replay(seeded_state, demo_events, extractor)  # mutates in place, returns deltas
    graph = project_graph(seeded_state, None)

    rows = [
        row
        for _, _, data in graph.edges(data=True)
        for row in data.get("directed_relations", [])
    ]
    triples = [(r["source"], r["relation"], r["target"]) for r in rows]
    assert len(triples) > len(set(triples)), "fixture no longer exercises the duplicate case"

    lines = edge_lines(subgraph_dot(graph, title="demo"))
    assert len(lines) == len(set(lines)), "duplicate arrows in the DOT output"
    assert len(lines) == len(set(triples))
