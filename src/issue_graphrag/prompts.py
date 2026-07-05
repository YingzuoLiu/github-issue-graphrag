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
You are summarizing a community in a repository knowledge graph.

Use the provided entities and relationships to write a concise community report.
Focus on:
- The main technical theme of this community
- Important entities and how they relate
- Why this community matters for issue analysis or contribution planning
- Any uncertainty or missing evidence

Return JSON:
{
  "title": "short title",
  "summary": "clear summary grounded in the graph",
  "rating": 1.0
}

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
