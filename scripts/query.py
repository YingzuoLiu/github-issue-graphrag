from __future__ import annotations

import argparse

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
- Mention source issue numbers or source IDs when useful.
- If evidence is insufficient, say what is missing.
- Keep the answer concise and technical.

Question:
{question}

Retrieved context:
{context}

Answer:
""".strip()


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


def load_processed_data():
    settings = load_settings()
    processed = settings.processed_data_dir

    graph = read_graph(processed / "graph.json")
    text_units = [TextUnit.model_validate(x) for x in read_json(processed / "text_units.json")]
    reports = [CommunityReport.model_validate(x) for x in read_json(processed / "community_reports.json")]

    return graph, text_units, reports


def main() -> None:
    parser = argparse.ArgumentParser(description="Query the GitHub Issue GraphRAG index.")
    parser.add_argument("question")
    parser.add_argument("--mode", choices=["auto", "naive", "local", "global"], default="auto")
    parser.add_argument("--answer", action="store_true", help="Generate a grounded LLM answer from retrieved context.")
    parser.add_argument("--show-context", action="store_true", help="Print retrieved context even when --answer is used.")
    args = parser.parse_args()

    graph, text_units, reports = load_processed_data()

    mode = args.mode
    if mode == "auto":
        # Simple routing for the MVP:
        # broad overview questions use global, specific technical questions use local.
        lowered = args.question.lower()
        if any(word in lowered for word in ["main", "overview", "themes", "opportunities", "summarize"]):
            mode = "global"
        else:
            mode = "local"

    print(f"Mode: {mode}\n")

    if mode == "naive":
        results = naive_search(text_units, args.question, top_k=8)
    elif mode == "local":
        results = local_search(graph, reports, text_units, args.question, top_k=8)
    elif mode == "global":
        results = global_search(reports, args.question, top_k=8)
    else:
        raise ValueError(f"Unsupported mode: {mode}")

    context = "\n\n".join(
        f"### {result.id} | score={result.score:.3f}\n\n{result.text}"
        for result in results
    )

    if args.answer:
        llm = make_llm()
        prompt = ANSWER_PROMPT.replace("{question}", args.question).replace("{context}", context)
        answer = llm.complete(prompt)

        print("Answer:\n")
        print(answer)

        if args.show_context:
            print("\n\nRetrieved context:\n")
            print(context)
        return

    for result in results:
        print(f"### {result.id} | score={result.score:.3f}\n")
        print(result.text)
        print("\n---\n")


if __name__ == "__main__":
    main()
