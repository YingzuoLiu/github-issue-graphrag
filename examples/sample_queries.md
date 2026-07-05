# Sample Queries

Use these after building an index from a small GitHub repository sample.

## Naive lookup

```bash
python scripts/query.py "What does issue #875 propose?" --mode naive
```

## Local graph search

```bash
python scripts/query.py "How is RRF related to hybrid retrieval?" --mode local
python scripts/query.py "Which modules may be affected by issue #875?" --mode local
python scripts/query.py "Are BM25 and vector search connected in the graph?" --mode local
```

## Global community search

```bash
python scripts/query.py "What are the main technical themes in this repository?" --mode global
python scripts/query.py "What contribution areas look promising?" --mode global
python scripts/query.py "What common architecture problems appear across the issues?" --mode global
```

## Auto routing

```bash
python scripts/query.py "这个 repo 主要有哪些值得贡献的方向？"
python scripts/query.py "RRF 会影响哪些 retrieval 相关组件？"
```
