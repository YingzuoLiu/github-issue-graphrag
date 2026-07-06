ENTITY_EXTRACTION_PROMPT = """
You are building a lightweight knowledge graph from GitHub issues, pull requests, and repository documents.

Extract technical entities and relationships from the input text.

Entity guidelines:
- Prefer technical concepts, repository modules, algorithms, APIs, features, bugs, tools, files, and issue/PR identifiers.
- Do not extract generic words unless they are clearly important in the repository context.
- Normalize obvious aliases when possible, for example RRF and Reciprocal Rank Fusion.

Relationship guidelines:
- Use concise relation labels such as proposes, affects, depends_on, fixes, improves, conflicts_with, uses, combines, mentions.
- Keep relationships grounded in the text.
- Include source evidence in the description when useful.

Return strict JSON with this shape:
{
  "entities": [
    {"name": "RRF", "type": "ALGORITHM", "description": "Reciprocal Rank Fusion for combining ranked retrieval results"}
  ],
  "relationships": [
    {"source": "RRF", "target": "Hybrid Retrieval", "relation": "improves", "description": "RRF is proposed to improve hybrid retrieval result fusion", "weight": 1.0}
  ]
}

Input text:
{text}
""".strip()


COMMUNITY_REPORT_PROMPT = """
You are summarizing one community from a GitHub repository knowledge graph.

The report will be used for:
- global GraphRAG search
- contribution opportunity analysis
- repository issue triage
- project demo explanation

Use the provided entities and relationships only. Do not invent issue numbers, files, or features.

Write a concise but useful technical report.

Focus on:
1. Technical theme: what this community is mainly about.
2. Key entities: the important files, features, APIs, modules, tools, or issues.
3. Contribution opportunities: concrete implementation/debugging opportunities suggested by the graph.
4. Evidence and uncertainty: what is supported by the graph, and what remains unclear.

Return strict JSON with this shape:
{
  "title": "short specific title",
  "summary": "Use markdown bullets. Include: Technical theme, Key entities, Contribution opportunities, Evidence / uncertainty.",
  "rating": 4.0
}

Rating guide:
- 5.0 = highly actionable contribution area with clear implementation path
- 4.0 = useful technical theme with several connected entities
- 3.0 = relevant but needs more evidence
- 2.0 = narrow or weakly connected
- 1.0 = singleton / low-value community

Community data:
{community_data}
""".strip()



LOCAL_ANSWER_PROMPT = """
Answer the user's question using the provided local GraphRAG context.

Use the context sections carefully:
- Reports give high-level background.
- Entities identify relevant concepts.
- Relationships describe graph edges.
- Sources provide grounding text.

If the context is insufficient, say what is missing. Do not invent evidence.

Question:
{question}

Context:
{context}
""".strip()


GLOBAL_MAP_PROMPT = """
Given the user question and one or more community reports, extract key points that help answer the question.

Return strict JSON:
{
  "points": [
    {"description": "point grounded in the reports", "score": 80}
  ]
}

Use score 0 if the reports do not help answer the question.

Question:
{question}

Community reports:
{reports}
""".strip()


GLOBAL_REDUCE_PROMPT = """
Synthesize the ranked analyst points into a final answer.

Requirements:
- Merge duplicate points.
- Keep the answer grounded in the provided points.
- State uncertainty when evidence is weak.
- Prefer a structured answer with concise bullets.

Question:
{question}

Ranked points:
{points}
""".strip()
