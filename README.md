# GitHub Issue GraphRAG

A lightweight GraphRAG pipeline for analyzing GitHub repositories through issues, pull requests, and documentation.

The project builds an entity-relation graph from GitHub issue discussions and repository documents, then supports three query modes:

- **Naive search**: standard chunk retrieval baseline.
- **Local graph search**: entity-centric retrieval for issue, module, and design-decision questions.
- **Global community search**: community-report summarization for repository-level themes and contribution opportunities.

## Motivation

Traditional RAG retrieves similar text chunks. That is useful for direct lookup, but it often struggles with questions that require cross-issue reasoning, module impact analysis, or repository-level summarization.

This project explores whether a lightweight GraphRAG pipeline can help answer questions such as:

- What are the main technical themes in this repository?
- Which modules may be affected by this issue?
- How are retrieval-related issues connected?
- What contribution areas look promising?
- Are there related issues or pull requests that may conflict with this change?

## Planned pipeline

```text
GitHub issues / PRs / README / docs
  -> TextUnits
  -> Entity & Relationship Extraction
  -> Knowledge Graph
  -> Community Detection
  -> Community Reports
  -> Naive / Local / Global Query
```

## Current scope

This repository starts with a minimal, readable implementation rather than a full production GraphRAG system.

The first milestone is to support:

1. Loading a small set of GitHub issues and repository documents.
2. Splitting content into TextUnits.
3. Extracting entities and relationships with an LLM.
4. Building a NetworkX-based knowledge graph.
5. Running simple local graph search and global community search.
6. Comparing GraphRAG retrieval against a naive chunk-search baseline.

## Repository layout

```text
github-issue-graphrag/
  data/
    raw/                 # raw GitHub issue/doc exports, ignored by git
    processed/           # generated indexes, ignored by git
  examples/
    sample_queries.md
  scripts/
    fetch_github_issues.py
    build_index.py
    query.py
  src/
    issue_graphrag/
      ingest/            # GitHub/data loading
      indexing/          # extraction, graph building, community reports
      retrieval/         # naive/local/global query modes
      storage/           # local JSON persistence
      llm/               # LLM client abstraction
  tests/
```

## Query modes

### Naive search

Uses text chunks directly as the retrieval unit. This is the baseline.

```text
query -> top-k TextUnits -> answer
```

### Local graph search

Starts from matched entities, expands through graph neighbors, then uses related relationships and source TextUnits as context.

```text
query -> entities -> graph neighbors -> relationships + sources -> answer
```

### Global community search

Uses community reports to answer broad repository-level questions.

```text
query -> community reports -> map key points -> reduce final answer
```

## Status

Work in progress. The current codebase is an initial skeleton for building the MVP.

## Safety notes

Do not commit API keys, private issue exports, resumes, or personal data. Keep `.env` and generated data files local.
