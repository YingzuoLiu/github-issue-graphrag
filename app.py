from __future__ import annotations

import streamlit as st

from issue_graphrag.config import load_settings
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient
from issue_graphrag.models import CommunityReport, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import naive_search
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


def route_query(mode: str, question: str, graph, text_units, reports):
    if mode == "auto":
        lowered = question.lower()
        if any(word in lowered for word in ["main", "overview", "themes", "opportunities", "summarize"]):
            mode = "global"
        else:
            mode = "local"

    if mode == "naive":
        results = naive_search(text_units, question, top_k=8)
    elif mode == "local":
        results = local_search(graph, reports, text_units, question, top_k=8)
    elif mode == "global":
        results = global_search(reports, question, top_k=8)
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    context = "\n\n".join(
        f"### {result.id} | score={result.score:.3f}\n\n{result.text}"
        for result in results
    )

    return mode, context


st.set_page_config(page_title="GitHub Issue GraphRAG", layout="wide")

st.title("GitHub Issue GraphRAG")
st.caption("Analyze GitHub issues with entity graphs, community reports, and grounded answers.")

with st.sidebar:
    st.header("Settings")
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

question = st.text_area("Question", value=demo, height=100)

if st.button("Run query", type="primary"):
    graph, text_units, reports = load_processed_data()

    with st.spinner("Retrieving GraphRAG context..."):
        resolved_mode, context = route_query(mode, question, graph, text_units, reports)

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
else:
    st.info("Choose a demo question or enter your own, then click Run query.")
