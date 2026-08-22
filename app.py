from __future__ import annotations

import streamlit as st

from issue_graphrag.config import load_settings
from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.events import EventLog
from issue_graphrag.live.history import timeline
from issue_graphrag.live.ontology import describe
from issue_graphrag.live.projection import event_subgraph, project_graph
from issue_graphrag.live.store import read_state
from issue_graphrag.live.viz import legend_markdown, subgraph_dot
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient
from issue_graphrag.models import CommunityReport, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import naive_search
from issue_graphrag.retrieval.router import route_query
from issue_graphrag.storage.json_store import read_graph, read_json


ANSWER_PROMPT = """
You are answering a question about a GitHub repository using retrieved GraphRAG context.

Rules:
- Use only the provided context.
- Prefer the Sources section over relationship edge direction when they conflict.
- Graph relationships may contain noisy direction, so rely on descriptions and source snippets for factual claims.
- Mention source issue numbers when useful.
- Avoid citing bare community IDs such as Source: 5.
- If evidence is insufficient, say what is missing.
- Keep the answer concise and technical.

Question:
{question}

Retrieved context:
{context}

Answer:
""".strip()


@st.cache_resource
def load_processed_data():
    settings = load_settings()
    processed = settings.processed_data_dir

    graph = read_graph(processed / "graph.json")
    text_units = [TextUnit.model_validate(x) for x in read_json(processed / "text_units.json")]
    reports = [CommunityReport.model_validate(x) for x in read_json(processed / "community_reports.json")]

    return graph, text_units, reports


BATCH_INDEX_FILES = ("graph.json", "text_units.json", "community_reports.json")


def missing_batch_index() -> list[str]:
    """Batch-index files that have not been built yet, in load order."""
    processed = load_settings().processed_data_dir
    return [name for name in BATCH_INDEX_FILES if not (processed / name).exists()]


@st.cache_data(show_spinner=False)
def load_live_index(state_mtime: float, log_mtime: float):
    """Load the live index. Cache keys are file mtimes so a replay invalidates it."""
    settings = load_settings()
    state = read_state(settings.processed_data_dir / "live_state.json")
    events = EventLog(settings.processed_data_dir / "event_log.jsonl").read_all()
    return state, events


def live_index_paths():
    settings = load_settings()
    return (
        settings.processed_data_dir / "live_state.json",
        settings.processed_data_dir / "event_log.jsonl",
    )


def make_llm():
    settings = load_settings()

    if settings.llm_provider == "mock":
        return MockLLMClient()

    if settings.llm_provider == "openai-compatible":
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError(
                "LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required for openai-compatible provider."
            )
        return OpenAICompatibleClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )

    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")


def retrieve_context(mode: str, question: str, graph, text_units, reports):
    resolved_mode = route_query(question, mode)

    if resolved_mode == "naive":
        results = naive_search(text_units, question, top_k=8)
    elif resolved_mode == "local":
        results = local_search(graph, reports, text_units, question, top_k=8)
    elif resolved_mode == "global":
        results = global_search(reports, question, top_k=8)
    else:
        raise ValueError(f"Unsupported mode: {resolved_mode}")

    context = "\n\n".join(
        f"### {result.id} | score={result.score:.3f}\n\n{result.text}"
        for result in results
    )

    return resolved_mode, context


def render_ask_tab(mode: str, generate_answer: bool, show_context: bool, question: str) -> None:
    missing = missing_batch_index()
    if missing:
        # The live tab works straight from a clone; the batch index does not, so
        # say which file is absent instead of raising FileNotFoundError at the
        # user through a Streamlit traceback.
        st.warning(
            "No batch index yet — `"
            + "`, `".join(missing)
            + "` "
            + ("is" if len(missing) == 1 else "are")
            + " missing. Build one with:\n\n"
            "```bash\n"
            "python scripts/fetch_github_issues.py trustgraph-ai/trustgraph --state open --limit 20\n"
            "python scripts/build_index.py trustgraph-ai__trustgraph_issues.json\n"
            "```\n\n"
            "This step calls the configured LLM provider. The **Live contribution graph** "
            "tab needs no index and no API key."
        )
        return

    if not st.button("Run query", type="primary"):
        st.info("Choose a demo question or enter your own, then click Run query.")
        return

    graph, text_units, reports = load_processed_data()

    with st.spinner("Retrieving GraphRAG context..."):
        resolved_mode, context = retrieve_context(mode, question, graph, text_units, reports)

    st.markdown(f"**Resolved mode:** `{resolved_mode}`")

    if generate_answer:
        with st.spinner("Generating grounded answer..."):
            llm = make_llm()
            prompt = ANSWER_PROMPT.replace("{question}", question).replace("{context}", context)
            answer = llm.complete(prompt)

        st.subheader("Answer")
        st.markdown(answer)

    if show_context:
        st.subheader("Retrieved context")
        st.text_area("Context", value=context, height=600)


def render_opportunities(graph, caption: str) -> None:
    ranked = opportunities(graph)
    st.subheader("Contribution opportunities")
    st.caption(caption)

    if not ranked:
        st.info("No open contribution opportunities at this point in the timeline.")
        return

    st.dataframe(
        [
            {
                "issue": item.node,
                "score": item.score,
                "status": item.status,
                "title": item.title,
                "concepts": ", ".join(item.concepts),
                "assignees": ", ".join(item.assignees),
                "claimed by": ", ".join(item.claimed_by),
                "blocked by": ", ".join(item.blocked_by),
            }
            for item in ranked
        ],
        hide_index=True,
        use_container_width=True,
    )

    for item in ranked:
        with st.expander(f"Why {item.node} scores {item.score:.2f} ({item.status})"):
            for reason in item.reasons:
                st.markdown(f"- {reason}")
            st.markdown("**Evidence**")
            for evidence in item.evidence:
                if evidence.url:
                    st.markdown(f"- [{evidence.label}]({evidence.url})")
                else:
                    st.markdown(f"- {evidence.label}")


def render_change_log(delta) -> None:
    added = delta.changes_of("added")
    updated = delta.changes_of("updated")
    invalidated = delta.changes_of("invalidated")
    superseded = delta.changes_of("superseded")

    left, right = st.columns(2)
    with left:
        st.markdown(f"**New facts ({len(added)})**")
        for fact in added:
            st.markdown(f"- `{fact.origin}` {fact.label()}")
        if not added:
            st.caption("none")
        if updated:
            st.markdown(f"**Re-asserted with new evidence ({len(updated)})**")
            for fact in updated:
                st.markdown(f"- `{fact.origin}` {fact.label()}")
    with right:
        st.markdown(f"**Invalidated facts ({len(invalidated)})**")
        for fact in invalidated:
            st.markdown(f"- `{fact.origin}` {fact.label()}")
        if not invalidated:
            st.caption("none")
        if superseded:
            st.markdown(f"**Superseded versions ({len(superseded)})**")
            st.caption(
                "Still true, but the evidence behind them moved, so the old "
                "version was closed and a new one opened."
            )

    if delta.opportunity_changes:
        st.markdown("**Recommendation changes**")
        for change in delta.opportunity_changes:
            before = f"{change.before_status} / {change.before_score}" if change.before_status else "—"
            after = f"{change.after_status} / {change.after_score}" if change.after_status else "—"
            st.markdown(f"- **{change.node}** {change.change}: {before} → {after}")
            for reason in change.reasons:
                st.caption(f"    because {reason}")


def render_evidence(graph, delta) -> None:
    """Every inferred edge must be traceable to a comment, issue body or PR."""
    edges = sorted(
        {
            (str(source), str(target))
            for source, target in graph.edges
            if graph.edges[source, target].get("change") in ("added", "invalidated")
        }
    )
    if not edges:
        st.caption("This event changed no edges.")
        return

    label = st.selectbox(
        "Inspect the evidence behind an edge",
        [f"{source} — {target}" for source, target in edges],
    )
    source, target = edges[[f"{s} — {t}" for s, t in edges].index(label)]
    data = graph.edges[source, target]

    for row in data.get("directed_relations", []):
        origin = "stated by GitHub" if row["origin"] == "github" else "inferred"
        st.markdown(f"**{row['source']} --{row['relation']}--> {row['target']}** ({origin})")

    for item in data.get("evidence", []):
        url = item.get("url")
        ref = f"[{item['ref']}]({url})" if url else item["ref"]
        st.markdown(f"- `{item['kind']}` {ref}")
        if item.get("snippet"):
            st.caption(f"\u201c{item['snippet']}\u201d")


def render_live_tab() -> None:
    state_path, log_path = live_index_paths()
    if not state_path.exists():
        st.warning(
            "No live index yet. Build one with:\n\n"
            "```bash\npython scripts/replay_events.py --verify-rebuild\n```"
        )
        return

    state, events = load_live_index(
        state_path.stat().st_mtime,
        log_path.stat().st_mtime if log_path.exists() else 0.0,
    )
    views = timeline(state, events)
    current = project_graph(state)

    columns = st.columns(6)
    columns[0].metric("Items", len(state.items))
    columns[1].metric("Nodes", current.number_of_nodes())
    columns[2].metric("Edges", current.number_of_edges())
    columns[3].metric("Facts", len(state.facts))
    columns[4].metric("Invalidated", sum(1 for fact in state.facts if fact.valid_to))
    columns[5].metric("Deliveries", len(state.processed_deliveries))

    if not views:
        st.info("The index has been seeded but no events have been replayed yet.")
        render_opportunities(current, "Current ranking.")
        return

    options = ["Now (all events applied)"] + [
        f"{index + 1}. {view.event.summary()} @ {view.after_moment}"
        for index, view in enumerate(views)
    ]
    choice = st.select_slider("Point in the event timeline", options=options, value=options[-1])

    if choice == options[0]:
        render_opportunities(current, "Every replayed delivery applied.")
        return

    view = views[options.index(choice) - 1]
    event = view.event

    st.markdown(
        f"**Delivery `{event.delivery_id}` — {event.summary()}** indexed at "
        f"{view.after_moment}  \n"
        f"Affected the graph between `{view.before_moment}` and `{view.after_moment}`. "
        f"GitHub reported it at `{event.received_at}`."
    )

    hops = st.slider("Neighbourhood hops", min_value=1, max_value=3, value=1)
    subgraph = event_subgraph(state, view.delta, hops=hops, moment=view.after_moment)

    st.graphviz_chart(subgraph_dot(subgraph, title=f"{event.summary()} ({event.delivery_id})"))
    st.caption(legend_markdown())

    render_change_log(view.delta)

    with st.expander("Evidence for the edges this event touched", expanded=False):
        render_evidence(subgraph, view.delta)

    render_opportunities(
        project_graph(state, view.after_moment),
        f"Ranking as it stood at {view.after_moment}.",
    )


st.set_page_config(page_title="GitHub Issue GraphRAG", layout="wide")

st.title("GitHub Issue GraphRAG")
st.caption(
    "A repository intelligence graph: ask grounded questions about issues, and watch "
    "the contribution graph change as events arrive."
)

with st.sidebar:
    st.header("Ask tab settings")
    mode = st.selectbox("Retrieval mode", ["auto", "local", "global", "naive"], index=0)
    generate_answer = st.checkbox("Generate grounded answer", value=True)
    show_context = st.checkbox("Show retrieved context", value=True)

    st.divider()
    st.markdown("### Demo questions")
    demo = st.radio(
        "Pick one",
        [
            "Why is graph-rag slow and which components are involved?",
            "How can TrustGraph improve document retrieval with hybrid retrieval?",
            "What is the Kafka backend issue about?",
            "What are the main technical contribution opportunities in this repo?",
        ],
        index=0,
    )

    st.divider()
    with st.expander("Graph ontology"):
        schema = describe()
        st.markdown("**Node types**")
        for node_type in schema["node_types"]:
            st.caption(f"`{node_type['name']}` ({node_type['origin']}) — {node_type['description']}")
        st.markdown("**Predicates**")
        for predicate in schema["predicates"]:
            st.caption(f"`{predicate['name']}` ({predicate['origin']}) — {predicate['description']}")

ask_tab, live_tab = st.tabs(["Ask", "Live contribution graph"])

with ask_tab:
    question = st.text_area("Question", value=demo, height=100)
    render_ask_tab(mode, generate_answer, show_context, question)

with live_tab:
    render_live_tab()
