# Retrieval evaluation results

Configuration: `top_k=8`, `repeats=3`, `fusion_depth=20`, `rrf_k=60`.

## Evaluation note

Evaluation note:
- `naive` is the BM25 lexical baseline.
- `local` is the current query-dependent local GraphRAG path.
- `global` currently selects top-rated community reports without query-dependent ranking, so it is
  evaluated by default only for `global_theme` questions.
- Global source recall covers documents attached to the selected top-k reports; source MRR is not
  reported because source ordering inside a community report is not a retrieval ranking.
- `vector` ranks TextUnits from the prebuilt embedded Qdrant collection.
- `hybrid` fuses reusable BM25 and vector rankings with RRF; raw lexical and cosine scores are not
  mixed directly.
- Community metrics are reported only for `local` and `global`, which return ranked community
  reports. TextUnit modes report these metrics as `n/a`.
- Reported query latency reuses initialized retrievers. One-time BM25 construction, embedding
  model loading, and Qdrant opening are reported separately as setup timings.

## One-time setup timings

| setup step | ms |
|---|---:|
| bm25_index_build | 8.60 |
| embedding_model_load | 19555.18 |
| qdrant_open | 27.21 |

## Summary by mode

| mode | queries | entity recall | source R@K | source MRR | community recall | community MRR | median ms | noise |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| hybrid | 12 | 0.847 | 0.944 | 0.882 | n/a | n/a | 12.18 | 61.8 |
| naive | 12 | 0.847 | 0.944 | 0.861 | n/a | n/a | 0.17 | 61.3 |
| vector | 12 | 0.731 | 0.903 | 0.778 | n/a | n/a | 9.24 | 62.2 |

## Detailed results

| query | category | mode | entity recall | source R@K | source MRR | community recall | ms | missing sources | missing entities |
|---|---|---|---:|---:|---:|---:|---:|---|---|
| graph_rag_latency | local_relationship | naive | 1.000 | 1.000 | 1.000 | n/a | 0.12 | - | - |
| graph_rag_latency | local_relationship | vector | 1.000 | 1.000 | 1.000 | n/a | 9.20 | - | - |
| graph_rag_latency | local_relationship | hybrid | 1.000 | 1.000 | 1.000 | n/a | 9.27 | - | - |
| kafka_backend_issue | single_lookup | naive | 1.000 | 1.000 | 1.000 | n/a | 0.19 | - | - |
| kafka_backend_issue | single_lookup | vector | 1.000 | 1.000 | 1.000 | n/a | 8.67 | - | - |
| kafka_backend_issue | single_lookup | hybrid | 1.000 | 1.000 | 1.000 | n/a | 11.37 | - | - |
| hybrid_retrieval_design | local_relationship | naive | 0.833 | 1.000 | 1.000 | n/a | 0.15 | - | Elasticsearch |
| hybrid_retrieval_design | local_relationship | vector | 0.833 | 1.000 | 0.500 | n/a | 9.29 | - | Elasticsearch |
| hybrid_retrieval_design | local_relationship | hybrid | 0.833 | 1.000 | 1.000 | n/a | 9.90 | - | Elasticsearch |
| document_rag_reranker | local_relationship | naive | 1.000 | 1.000 | 1.000 | n/a | 0.18 | - | - |
| document_rag_reranker | local_relationship | vector | 0.800 | 1.000 | 1.000 | n/a | 12.03 | - | RerankerClient |
| document_rag_reranker | local_relationship | hybrid | 1.000 | 1.000 | 1.000 | n/a | 15.57 | - | - |
| cross_encoder_reranking | local_relationship | naive | 0.833 | 1.000 | 1.000 | n/a | 0.40 | - | BGE-reranker |
| cross_encoder_reranking | local_relationship | vector | 0.833 | 1.000 | 1.000 | n/a | 10.21 | - | BGE-reranker |
| cross_encoder_reranking | local_relationship | hybrid | 0.833 | 1.000 | 1.000 | n/a | 15.48 | - | BGE-reranker |
| workspace_export_import | single_lookup | naive | 1.000 | 1.000 | 1.000 | n/a | 0.21 | - | - |
| workspace_export_import | single_lookup | vector | 1.000 | 1.000 | 1.000 | n/a | 14.43 | - | - |
| workspace_export_import | single_lookup | hybrid | 1.000 | 1.000 | 1.000 | n/a | 14.58 | - | - |
| config_as_code | local_relationship | naive | 0.800 | 1.000 | 0.500 | n/a | 0.16 | - | tg-apply-config |
| config_as_code | local_relationship | vector | 0.200 | 1.000 | 0.333 | n/a | 11.87 | - | tg-apply-config, manifest.yaml, config-svc, tg-put-config-items |
| config_as_code | local_relationship | hybrid | 0.800 | 1.000 | 1.000 | n/a | 11.32 | - | tg-apply-config |
| image_to_text_service | single_lookup | naive | 1.000 | 1.000 | 1.000 | n/a | 0.24 | - | - |
| image_to_text_service | single_lookup | vector | 1.000 | 1.000 | 1.000 | n/a | 8.96 | - | - |
| image_to_text_service | single_lookup | hybrid | 1.000 | 1.000 | 1.000 | n/a | 9.81 | - | - |
| semantic_chunking | single_lookup | naive | 0.600 | 1.000 | 1.000 | n/a | 0.10 | - | spaCy, chunking/semantic/chunker.py |
| semantic_chunking | single_lookup | vector | 0.600 | 1.000 | 1.000 | n/a | 8.98 | - | spaCy, chunking/semantic/chunker.py |
| semantic_chunking | single_lookup | hybrid | 0.600 | 1.000 | 1.000 | n/a | 14.27 | - | spaCy, chunking/semantic/chunker.py |
| ontology_rag_extraction | local_relationship | naive | 1.000 | 1.000 | 1.000 | n/a | 0.16 | - | - |
| ontology_rag_extraction | local_relationship | vector | 0.400 | 0.500 | 0.167 | n/a | 8.54 | trustgraph-ai/trustgraph#issue-911 | extract.py, kg-extract-definitions, kg-extract-relationships |
| ontology_rag_extraction | local_relationship | hybrid | 1.000 | 1.000 | 0.333 | n/a | 14.82 | - | - |
| docker_deployment | global_theme | naive | 0.600 | 1.000 | 0.333 | n/a | 0.16 | - | apachepulsar/pulsar:4.1.0, qdrant/qdrant:v1.16.0 |
| docker_deployment | global_theme | vector | 0.600 | 1.000 | 0.333 | n/a | 8.62 | - | apachepulsar/pulsar:4.1.0, qdrant/qdrant:v1.16.0 |
| docker_deployment | global_theme | hybrid | 0.600 | 1.000 | 1.000 | n/a | 10.00 | - | apachepulsar/pulsar:4.1.0, qdrant/qdrant:v1.16.0 |
| repo_contribution_opportunities | global_theme | naive | 0.500 | 0.333 | 0.500 | n/a | 0.17 | trustgraph-ai/trustgraph#issue-875, trustgraph-ai/trustgraph#issue-878, trustgraph-ai/trustgraph#issue-922, trustgraph-ai/trustgraph#issue-944 | Hybrid Retrieval, Kafka, TrustGraph Configuration Builder |
| repo_contribution_opportunities | global_theme | vector | 0.500 | 0.333 | 1.000 | n/a | 11.43 | trustgraph-ai/trustgraph#issue-877, trustgraph-ai/trustgraph#issue-919, trustgraph-ai/trustgraph#issue-922, trustgraph-ai/trustgraph#issue-944 | Kafka, Workspace Export/Import, TrustGraph Configuration Builder |
| repo_contribution_opportunities | global_theme | hybrid | 0.500 | 0.333 | 0.250 | n/a | 12.99 | trustgraph-ai/trustgraph#issue-875, trustgraph-ai/trustgraph#issue-878, trustgraph-ai/trustgraph#issue-922, trustgraph-ai/trustgraph#issue-944 | Hybrid Retrieval, Kafka, TrustGraph Configuration Builder |
