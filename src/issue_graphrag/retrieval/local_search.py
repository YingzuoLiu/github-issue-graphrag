from __future__ import annotations

import re
from collections import defaultdict

import networkx as nx

from issue_graphrag.models import CommunityReport, SearchResult, TextUnit


_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "in", "on", "with", "and", "or", "by", "from",
    "how", "what", "why", "which", "who", "where", "when", "can", "could",
    "should", "would", "about", "main", "repo", "repository", "issue",
    "issues", "technical", "contribution", "opportunities",
}


_QUERY_EXPANSIONS = {
    "graph": {"graph", "graph-rag", "graph_rag", "graphrag"},
    "rag": {"rag", "graph-rag", "document-rag", "graph_rag", "document_rag"},
    "graph-rag": {"graph-rag", "graph_rag", "graph", "rag"},
    "document-rag": {"document-rag", "document_rag", "document", "rag"},
    "hybrid": {"hybrid", "bm25", "tfidf", "tf-idf", "rrf", "vector"},
    "retrieval": {"retrieval", "rag", "search", "ranking", "reranking"},
    "kafka": {"kafka", "consumer", "producer", "unsubscribe", "offset", "pubsub"},
    "slow": {"slow", "latency", "round-trip", "roundtrips", "pulsar", "memgraph"},
}


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    raw = re.findall(r"[a-z0-9][a-z0-9_\-.]*", text)
    out: set[str] = set()

    for token in raw:
        if token in _STOPWORDS:
            continue

        out.add(token)

        # Also split snake/kebab/dot names:
        # graph_rag.Processor -> graph, rag, processor
        for part in re.split(r"[_\-.]+", token):
            if part and part not in _STOPWORDS:
                out.add(part)

    expanded = set(out)
    for token in list(out):
        expanded.update(_QUERY_EXPANSIONS.get(token, set()))

    return expanded


def _text_score(query_tokens: set[str], text: str) -> float:
    if not text:
        return 0.0

    text_tokens = _tokens(text)
    if not text_tokens:
        return 0.0

    overlap = query_tokens & text_tokens
    if not overlap:
        return 0.0

    # Reward overlap, but avoid overly long text dominating everything.
    return len(overlap) / (len(query_tokens) ** 0.5)


def _node_text(name: str, data: dict) -> str:
    return f"{name} {data.get('type', '')} {data.get('description', '')}"


def _edge_text(source: str, target: str, data: dict) -> str:
    relations = " ".join(data.get("relations", []))
    descriptions = " ".join(data.get("descriptions", []))
    return f"{source} {target} {relations} {descriptions}"


def _report_text(report: CommunityReport) -> str:
    return f"{report.title} {' '.join(report.entity_names)} {report.summary}"


def _shorten(text: str, max_len: int = 900) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def _rank_nodes(graph: nx.Graph, query_tokens: set[str]) -> list[tuple[str, float]]:
    ranked: list[tuple[str, float]] = []

    for node, data in graph.nodes(data=True):
        score = _text_score(query_tokens, _node_text(str(node), data))

        if score <= 0:
            continue

        # Mildly downweight giant hub nodes like TrustGraph unless directly queried.
        degree = graph.degree[node]
        if degree > 12 and str(node).lower() not in query_tokens:
            score *= 0.65

        ranked.append((str(node), score))

    return sorted(ranked, key=lambda item: item[1], reverse=True)


def _rank_edges(
    graph: nx.Graph,
    query_tokens: set[str],
    seed_nodes: set[str],
) -> list[tuple[str, str, dict, float]]:
    ranked: list[tuple[str, str, dict, float]] = []

    for source, target, data in graph.edges(data=True):
        source = str(source)
        target = str(target)

        text_score = _text_score(query_tokens, _edge_text(source, target, data))

        seed_bonus = 0.0
        if source in seed_nodes:
            seed_bonus += 0.8
        if target in seed_nodes:
            seed_bonus += 0.8

        # Give a smaller bonus to edges adjacent to seed nodes even if text overlap is weak.
        score = text_score + seed_bonus

        if score <= 0:
            continue

        ranked.append((source, target, data, score))

    return sorted(ranked, key=lambda item: item[3], reverse=True)


def _rank_reports(
    reports: list[CommunityReport],
    query_tokens: set[str],
    selected_nodes: set[str],
) -> list[tuple[CommunityReport, float]]:
    ranked: list[tuple[CommunityReport, float]] = []

    for report in reports:
        score = _text_score(query_tokens, _report_text(report))

        entity_overlap = selected_nodes & set(report.entity_names)
        score += min(len(entity_overlap), 5) * 0.4

        if score <= 0:
            continue

        ranked.append((report, score))

    return sorted(ranked, key=lambda item: item[1], reverse=True)


def _rank_sources(
    text_units: list[TextUnit],
    query_tokens: set[str],
    source_id_scores: dict[str, float],
) -> list[tuple[TextUnit, float]]:
    ranked: list[tuple[TextUnit, float]] = []

    for unit in text_units:
        score = source_id_scores.get(unit.id, 0.0)
        score += _text_score(query_tokens, unit.text)

        title = str(unit.metadata.get("document_title", ""))
        score += _text_score(query_tokens, title) * 1.5

        if score <= 0:
            continue

        ranked.append((unit, score))

    return sorted(ranked, key=lambda item: item[1], reverse=True)


def local_search(
    graph: nx.Graph,
    reports: list[CommunityReport],
    text_units: list[TextUnit],
    query: str,
    top_k: int = 8,
) -> list[SearchResult]:
    query_tokens = _tokens(query)

    ranked_nodes = _rank_nodes(graph, query_tokens)
    seed_nodes = {node for node, _ in ranked_nodes[: max(top_k, 8)]}

    ranked_edges = _rank_edges(graph, query_tokens, seed_nodes)
    selected_edges = ranked_edges[: max(top_k * 4, 24)]

    selected_nodes = set(seed_nodes)
    source_id_scores: dict[str, float] = defaultdict(float)

    for source, target, data, score in selected_edges:
        selected_nodes.add(source)
        selected_nodes.add(target)

        for source_id in data.get("source_ids", []):
            source_id_scores[source_id] += score

    # Add source ids from selected entities.
    for node in selected_nodes:
        if graph.has_node(node):
            data = graph.nodes[node]
            for source_id in data.get("source_ids", []):
                source_id_scores[source_id] += 0.5

    ranked_reports = _rank_reports(reports, query_tokens, selected_nodes)
    ranked_sources = _rank_sources(text_units, query_tokens, source_id_scores)

    # Dynamic thresholds: keep strong matches, drop weak incidental overlaps.
    max_report_score = ranked_reports[0][1] if ranked_reports else 0.0
    max_source_score = ranked_sources[0][1] if ranked_sources else 0.0

    min_node_score = 0.75
    min_edge_score = 1.0
    min_report_score = max(1.5, max_report_score * 0.55)
    min_source_score = max(2.0, max_source_score * 0.20)

    selected_node_rows = []
    for node, score in ranked_nodes[: max(top_k * 3, 24)]:
        if node not in selected_nodes or score < min_node_score:
            continue
        data = graph.nodes[node]
        selected_node_rows.append(
            f"- {node} | score={score:.2f} | type={data.get('type', 'UNKNOWN')} | "
            f"degree={graph.degree[node]} | description={data.get('description', '')}"
        )

    selected_edge_rows = []
    for source, target, data, score in selected_edges:
        if score < min_edge_score:
            continue
        selected_edge_rows.append(
            f"- {source} -- {data.get('relations', [])} -- {target}; "
            f"score={score:.2f}; descriptions={data.get('descriptions', [])[:2]}"
        )

    selected_report_rows = []
    for report, score in ranked_reports[: min(top_k, 5)]:
        if score < min_report_score:
            continue
        selected_report_rows.append(
            f"[{report.id}] {report.title} | score={score:.2f}\n{report.summary}"
        )

    selected_source_rows = []
    for unit, score in ranked_sources:
        if score < min_source_score:
            continue
        selected_source_rows.append(
            f"[{unit.id}] {unit.metadata.get('document_title', '')} | score={score:.2f}\n"
            f"{_shorten(unit.text)}"
        )
        if len(selected_source_rows) >= min(top_k, 6):
            break

    context = "\n\n".join(
        [
            "-----Reports-----",
            "\n\n".join(selected_report_rows) or "(none)",
            "-----Entities-----",
            "\n".join(selected_node_rows) or "(none)",
            "-----Relationships-----",
            "\n".join(selected_edge_rows) or "(none)",
            "-----Sources-----",
            "\n\n".join(selected_source_rows) or "(none)",
        ]
    )

    return [SearchResult(id="local_context", score=1.0, text=context)]
