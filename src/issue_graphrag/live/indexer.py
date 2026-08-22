"""The incremental indexer.

One rule shapes this module: the *expensive* work is scoped, the *cheap* work is
not. LLM extraction runs only for documents whose text actually changed, while
deterministic facts are re-derived for every document on every event and the
graph is folded fresh from the fact set. Re-deriving strings and re-folding a
few thousand facts costs microseconds, and it removes an entire class of
ordering bugs — which is why an incremental replay and a full rebuild land on
the same graph by construction rather than by luck.

Two clocks are kept apart. ``effective_at`` on a record is the source clock and
decides which payload wins. The ingestion clock below decides when the index
learned something, and is what fact validity windows are keyed on. It is forced
to be monotonic: a delivery that arrives late still opens its validity window
*now*, because "what did the index believe at time T" must not be rewritten
retroactively.
"""

from __future__ import annotations

from issue_graphrag.live.contribution import diff_opportunities, opportunities
from issue_graphrag.live.documents import text_units_for
from issue_graphrag.live.extraction import Extractor, llm_facts_for_item
from issue_graphrag.live.facts import ItemIndex, github_facts_for_item
from issue_graphrag.live.models import (
    Fact,
    FactChange,
    GraphDelta,
    LiveState,
    RejectedFact,
    RepoEvent,
    RepoItem,
)
from issue_graphrag.live.ontology import validate_inferred
from issue_graphrag.live.projection import diff_graphs, project_graph
from issue_graphrag.live.records import UnsupportedEvent, apply_event_to_records
from issue_graphrag.live.store import reconcile_facts
from issue_graphrag.live.timeutil import max_iso, to_iso

SEED_DELIVERY = "seed"
REBUILD_DELIVERY = "rebuild"


class NullExtractor:
    """Applies only the deterministic layer. Useful for offline smoke runs."""

    def extract(self, text_units):  # noqa: ANN001 - matches the Extractor protocol
        from issue_graphrag.models import ExtractionResult

        return ExtractionResult()


class RecordedExtractor:
    """Replays extraction output already stored in a state.

    Used by the rebuild check so that it isolates the incremental bookkeeping
    from the model. Re-running a real LLM would conflate "did the index drift?"
    with "did the model answer the same way twice?", and only the first of those
    is a property this code can guarantee.
    """

    def __init__(self, state: LiveState):
        self._by_document: dict[str, list[Fact]] = {}
        for fact in state.valid_facts():
            if fact.origin == "llm":
                self._by_document.setdefault(fact.document_id, []).append(fact)

    def facts_for(self, document_id: str) -> list[Fact]:
        return [fact.model_copy(deep=True) for fact in self._by_document.get(document_id, [])]


def ingestion_moment(state: LiveState, event: RepoEvent) -> str:
    """The index clock: monotonic, never earlier than what we already recorded."""
    return max_iso(state.last_event_at, event.received_at) or event.received_at


def refresh_deterministic(state: LiveState, moment: str, delivery_id: str) -> list[FactChange]:
    """Re-derive GitHub-stated facts for every document.

    Doing this repo-wide rather than per-event is what makes cross-document
    references converge: an existing issue's ``#950`` mention only becomes an
    edge once PR #950 is known, and that must not depend on replay order.
    """
    index = ItemIndex(state.items)
    changes: list[FactChange] = []
    for document_id in sorted(state.items):
        item = state.items[document_id]
        facts = github_facts_for_item(
            item, index, moment, delivery_id, text_units=text_units_for(item)
        )
        changes.extend(reconcile_facts(state, document_id, "github", facts, moment, delivery_id))
    return changes


def _stale_documents(state: LiveState) -> list[str]:
    """Documents whose extraction input changed since they were last extracted."""
    return sorted(
        document_id
        for document_id, item in state.items.items()
        if state.extraction_signatures.get(document_id) != item.extraction_signature()
    )


def refresh_inferred(
    state: LiveState,
    extractor: Extractor | RecordedExtractor,
    moment: str,
    delivery_id: str,
) -> tuple[list[FactChange], list[RejectedFact], list[str]]:
    """Run scoped extraction and let the ontology decide what may be stored."""
    stale = _stale_documents(state)
    if not stale:
        return [], [], []

    changes: list[FactChange] = []
    rejected: list[RejectedFact] = []

    for document_id in stale:
        item = state.items[document_id]

        if isinstance(extractor, RecordedExtractor):
            candidates = extractor.facts_for(document_id)
        else:
            candidates = llm_facts_for_item(
                item, text_units_for(item), extractor, moment, delivery_id
            )

        kept, refused = validate_inferred(candidates)
        rejected.extend(RejectedFact(fact=fact, reason=reason) for fact, reason in refused)
        changes.extend(reconcile_facts(state, document_id, "llm", kept, moment, delivery_id))
        state.extraction_signatures[document_id] = item.extraction_signature()

    return changes, rejected, stale


def _refresh(
    state: LiveState,
    extractor: Extractor | RecordedExtractor,
    moment: str,
    delivery_id: str,
) -> tuple[list[FactChange], list[RejectedFact], list[str]]:
    changes = refresh_deterministic(state, moment, delivery_id)
    inferred, rejected, reextracted = refresh_inferred(state, extractor, moment, delivery_id)
    return changes + inferred, rejected, reextracted


def bootstrap(
    repo: str,
    items: dict[str, RepoItem],
    extractor: Extractor,
    moment: str | None = None,
) -> LiveState:
    """Build the initial index from a repository snapshot."""
    resolved = moment or max_iso(*[item.effective_at for item in items.values()])
    if not resolved:
        raise ValueError("snapshot has no timestamps; pass an explicit moment")

    state = LiveState(repo=repo, items=dict(items))
    _refresh(state, extractor, to_iso(resolved), SEED_DELIVERY)
    state.last_event_at = to_iso(resolved)
    return state


def apply_event(state: LiveState, event: RepoEvent, extractor: Extractor) -> GraphDelta:
    """Apply one delivery and report exactly what it changed."""
    moment = ingestion_moment(state, event)
    event.indexed_at = moment

    delta = GraphDelta(
        delivery_id=event.delivery_id,
        event_type=event.event_type,
        action=event.action,
        occurred_at=event.received_at,
        indexed_at=moment,
        repo=event.repo,
    )

    if state.has_delivery(event.delivery_id):
        delta.applied = False
        delta.skip_reason = "duplicate delivery"
        return delta

    before_graph = project_graph(state)
    before_opportunities = opportunities(before_graph)

    try:
        affected = apply_event_to_records(state, event)
    except (UnsupportedEvent, KeyError, TypeError, ValueError) as exc:
        state.processed_deliveries.append(event.delivery_id)
        delta.applied = False
        delta.skip_reason = str(exc)
        return delta

    changes, rejected, reextracted = _refresh(state, extractor, moment, event.delivery_id)

    after_graph = project_graph(state)
    graph_diff = diff_graphs(before_graph, after_graph)

    state.processed_deliveries.append(event.delivery_id)
    state.last_event_at = moment

    delta.affected_documents = sorted(affected)
    delta.reextracted_documents = reextracted
    delta.fact_changes = changes
    delta.rejected_inferred = rejected
    delta.added_nodes = graph_diff["added_nodes"]
    delta.removed_nodes = graph_diff["removed_nodes"]
    delta.added_edges = [tuple(edge) for edge in graph_diff["added_edges"]]
    delta.removed_edges = [tuple(edge) for edge in graph_diff["removed_edges"]]
    delta.changed_nodes = graph_diff["changed_nodes"]
    delta.opportunity_changes = diff_opportunities(
        before_opportunities, opportunities(after_graph)
    )
    return delta


def replay(state: LiveState, events: list[RepoEvent], extractor: Extractor) -> list[GraphDelta]:
    return [apply_event(state, event, extractor) for event in events]


def rebuild(
    state: LiveState,
    extractor: Extractor | None = None,
    moment: str | None = None,
) -> LiveState:
    """Re-index the current records from scratch, ignoring event history.

    With no extractor the recorded extraction output is replayed, which makes
    the comparison a proof about *this code*: the deterministic layer, the fact
    lifecycle and the projection must reach the same graph whether they got
    there in six steps or one.

    Passing an extractor re-runs extraction as well. That is a useful stability
    check for a deterministic extractor, but with a live model it measures the
    model's repeatability, not this pipeline's convergence, and should not be
    reported as a consistency guarantee.
    """
    resolved = moment or state.last_event_at
    if not resolved:
        raise ValueError("state has no timestamps; pass an explicit moment")

    fresh = LiveState(
        repo=state.repo,
        items={key: value.model_copy(deep=True) for key, value in state.items.items()},
    )
    _refresh(fresh, extractor or RecordedExtractor(state), to_iso(resolved), REBUILD_DELIVERY)
    fresh.last_event_at = to_iso(resolved)
    return fresh
