from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph
from pydantic import BaseModel


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_models(path: Path, models: list[BaseModel]) -> None:
    write_json(path, [m.model_dump() for m in models])


def write_graph(path: Path, graph: nx.Graph) -> None:
    write_json(path, json_graph.node_link_data(graph, edges="links"))


def read_graph(path: Path) -> nx.Graph:
    return json_graph.node_link_graph(read_json(path), edges="links")


# The three files the batch pipeline writes, in the order the app loads them.
BATCH_INDEX_FILES = ("graph.json", "text_units.json", "community_reports.json")


def missing_batch_index(processed_dir: Path) -> list[str]:
    """Batch-index files that have not been built yet, in load order.

    A fresh clone has none of them, and building them needs a provider key. Any
    caller that reads the batch index has to check this first: ``read_graph``
    raises ``FileNotFoundError``, which is the wrong thing to show a user who
    simply has not run ``build_index.py`` yet.
    """
    return [name for name in BATCH_INDEX_FILES if not (processed_dir / name).exists()]
