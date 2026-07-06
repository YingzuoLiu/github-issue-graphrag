from __future__ import annotations

import argparse

from issue_graphrag.config import load_settings
from issue_graphrag.storage.json_store import read_graph


def shorten(text: str, max_len: int = 220) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect graph relationships by label.")
    parser.add_argument("--relation", type=str, required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    settings = load_settings()
    graph = read_graph(settings.processed_data_dir / "graph.json")

    relation = args.relation.strip().lower().replace(" ", "_")
    count = 0

    print(f"=== Relations: {relation} ===")

    for source, target, data in graph.edges(data=True):
        relations = data.get("relations", [])
        if relation not in relations:
            continue

        count += 1
        print(f"\n[{count}] {source} -- {relation} -- {target}")

        descriptions = data.get("descriptions", [])
        for desc in descriptions[:3]:
            print(f"  - {shorten(desc)}")

        source_ids = data.get("source_ids", [])
        if source_ids:
            print(f"  source_ids={source_ids[:5]}")

        if count >= args.limit:
            break

    print(f"\nshown: {count}")


if __name__ == "__main__":
    main()
