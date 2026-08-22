"""Reconstruct what each event did, purely by querying fact validity windows.

Because facts carry ``valid_from`` and ``valid_to``, the effect of an event can
be recovered after the fact without re-running the indexer. That is what lets
the demo scrub back through the timeline, and it is the same mechanism behind
``--as-of`` queries.
"""

from __future__ import annotations

import networkx as nx
from pydantic import BaseModel

from issue_graphrag.live.contribution import diff_opportunities, opportunities
from issue_graphrag.live.models import FactChange, GraphDelta, LiveState, RepoEvent
from issue_graphrag.live.projection import diff_graphs, project_graph


class EventView(BaseModel):
    """One event plus the graph windows on either side of it."""

    event: RepoEvent
    before_moment: str
    after_moment: str
    delta: GraphDelta

    class Config:
        arbitrary_types_allowed = True


def reconstruct_delta(
    state: LiveState,
    event: RepoEvent,
    before_moment: str,
    before_graph: nx.Graph | None = None,
    after_graph: nx.Graph | None = None,
) -> GraphDelta:
    """Rebuild an event's delta from the fact windows it opened and closed."""
    moment = event.indexed_at or event.received_at
    before = before_graph if before_graph is not None else project_graph(state, before_moment)
    after = after_graph if after_graph is not None else project_graph(state, moment)

    changes: list[FactChange] = []
    for fact in sorted(state.facts, key=lambda item: item.key):
        closed = fact.valid_to == moment and fact.invalidated_by == event.delivery_id
        opened = fact.valid_from == moment and fact.asserted_by == event.delivery_id
        if closed and opened:
            # Same assertion, new version: closed and reopened by one event.
            continue
        if closed:
            superseded = any(
                other.key == fact.key and other.valid_from == moment for other in state.facts
            )
            changes.append(
                FactChange(change="superseded" if superseded else "invalidated", fact=fact)
            )
        elif opened:
            replaced = any(
                other.key == fact.key and other.valid_to == moment for other in state.facts
            )
            changes.append(FactChange(change="updated" if replaced else "added", fact=fact))

    graph_diff = diff_graphs(before, after)

    return GraphDelta(
        delivery_id=event.delivery_id,
        event_type=event.event_type,
        action=event.action,
        occurred_at=event.received_at,
        indexed_at=moment,
        repo=event.repo,
        fact_changes=changes,
        added_nodes=graph_diff["added_nodes"],
        removed_nodes=graph_diff["removed_nodes"],
        added_edges=[tuple(edge) for edge in graph_diff["added_edges"]],
        removed_edges=[tuple(edge) for edge in graph_diff["removed_edges"]],
        changed_nodes=graph_diff["changed_nodes"],
        opportunity_changes=diff_opportunities(opportunities(before), opportunities(after)),
    )


def timeline(state: LiveState, events: list[RepoEvent], seed_moment: str | None = None) -> list[EventView]:
    """Ordered views of every applied delivery, oldest first."""
    seen: set[str] = set()
    ordered: list[RepoEvent] = []
    for event in sorted(
        events, key=lambda item: (item.indexed_at or item.received_at, item.delivery_id)
    ):
        if event.delivery_id in seen:
            continue
        seen.add(event.delivery_id)
        ordered.append(event)

    previous = seed_moment or (
        (ordered[0].indexed_at or ordered[0].received_at) if ordered else None
    )
    if previous is None:
        return []

    # The window before the first event is the snapshot the index was seeded at.
    earliest_fact = min((fact.valid_from for fact in state.facts), default=previous)
    previous = seed_moment or earliest_fact

    views: list[EventView] = []
    graph = project_graph(state, previous)

    for event in ordered:
        moment = event.indexed_at or event.received_at
        after = project_graph(state, moment)
        views.append(
            EventView(
                event=event,
                before_moment=previous,
                after_moment=moment,
                delta=reconstruct_delta(state, event, previous, graph, after),
            )
        )
        previous, graph = moment, after

    return views
