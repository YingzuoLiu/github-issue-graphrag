"""Replay GitHub events into the live contribution graph.

Deterministic by design: events carry their own timestamps and delivery ids, so
the same fixture set always produces the same graph. Use --verify-rebuild to
prove the incremental path agrees with a from-scratch index.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from issue_graphrag.config import load_settings
from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.events import EventLog, load_events
from issue_graphrag.live.indexer import apply_event, bootstrap, rebuild
from issue_graphrag.live.models import GraphDelta, LiveState
from issue_graphrag.live.ontology import describe
from issue_graphrag.live.projection import graph_signature, project_graph
from issue_graphrag.live.repositories import (
    RepoRegistry,
    read_freshness,
    repo_paths,
    write_freshness,
)
from issue_graphrag.live.records import seed_items
from issue_graphrag.live.runtime import configured_extractor
from issue_graphrag.live.store import read_state, write_state

DEFAULT_SEED = Path("fixtures/live_demo/seed.json")
DEFAULT_EVENTS = Path("fixtures/live_demo/events")
DEFAULT_RULES = Path("fixtures/live_demo/extraction_rules.json")


def print_delta(delta: GraphDelta) -> None:
    header = f"[{delta.delivery_id}] {delta.event_type}.{delta.action} @ {delta.occurred_at}"
    print(f"\n{header}")
    print("-" * len(header))

    if not delta.applied:
        print(f"  skipped: {delta.skip_reason}")
        return

    print(f"  affected documents : {', '.join(delta.affected_documents) or '(none)'}")
    print(f"  re-extracted       : {', '.join(delta.reextracted_documents) or '(none, text unchanged)'}")

    for change in ("added", "updated", "superseded", "invalidated"):
        facts = delta.changes_of(change)
        for fact in facts:
            print(f"  {change:<12} [{fact.origin}] {fact.label()}")

    for rejected in delta.rejected_inferred:
        print(f"  rejected     [llm] {rejected.fact.label()} -- {rejected.reason}")

    if delta.added_nodes or delta.removed_nodes:
        print(f"  nodes +{len(delta.added_nodes)} -{len(delta.removed_nodes)}")
    if delta.added_edges or delta.removed_edges:
        print(f"  edges +{len(delta.added_edges)} -{len(delta.removed_edges)}")

    for change in delta.opportunity_changes:
        before = f"{change.before_status}/{change.before_score}" if change.before_status else "-"
        after = f"{change.after_status}/{change.after_score}" if change.after_status else "-"
        print(f"  recommendation {change.change}: {change.node} {before} -> {after}")
        for reason in change.reasons:
            print(f"      because {reason}")

    if delta.is_noop():
        print("  no change")


def print_opportunities(state: LiveState, limit: int) -> None:
    ranked = opportunities(project_graph(state))[:limit]
    print("\nContribution opportunities")
    print("=" * 26)
    if not ranked:
        print("  (none)")
        return
    for item in ranked:
        print(f"  {item.score:5.2f}  {item.status:<10} {item.node}  {item.title}")
        for reason in item.reasons:
            print(f"           - {reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED, help="repository snapshot JSON")
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="event file or directory")
    parser.add_argument(
        "--rules",
        type=Path,
        default=DEFAULT_RULES,
        help="fixture extraction rules; pass --llm to use a real model instead",
    )
    parser.add_argument("--llm", action="store_true", help="extract with the configured LLM provider")
    parser.add_argument("--state", type=Path, default=None, help="where to write live_state.json")
    parser.add_argument("--event-log", type=Path, default=None, help="where to append the event log")
    parser.add_argument("--resume", action="store_true", help="continue from an existing state file")
    parser.add_argument(
        "--verify-rebuild",
        action="store_true",
        help="rebuild from the records and prove the incremental path did not drift",
    )
    parser.add_argument(
        "--re-extract",
        action="store_true",
        help="also re-run extraction during --verify-rebuild (stability check, not a proof)",
    )
    parser.add_argument("--top", type=int, default=10, help="how many opportunities to print")
    parser.add_argument("--quiet", action="store_true", help="only print the final ranking")
    parser.add_argument("--no-write", action="store_true", help="do not persist state or event log")
    parser.add_argument("--show-ontology", action="store_true", help="print the schema and exit")
    args = parser.parse_args()

    if args.show_ontology:
        print(json.dumps(describe(), indent=2))
        return

    settings = load_settings()
    with args.seed.open("r", encoding="utf-8") as handle:
        snapshot = json.load(handle)
    repo = str(snapshot["repo"])
    registry = RepoRegistry(settings.repo_data_dir, settings.github_repos)
    repo_storage = repo_paths(settings.repo_data_dir, repo) if args.no_write else registry.register(repo)
    state_path = args.state or repo_storage.state
    log_path = args.event_log or repo_storage.event_log

    extractor = configured_extractor(
        rules=None if args.llm else args.rules,
        use_llm=args.llm,
    )

    if args.resume and state_path.exists():
        state = read_state(state_path)
        print(f"Resumed live state from {state_path} ({len(state.processed_deliveries)} deliveries)")
    else:
        state = bootstrap(repo_storage.repo, seed_items(repo_storage.repo, snapshot["items"]), extractor)
        graph = project_graph(state)
        print(
            f"Bootstrapped {repo}: {len(state.items)} items, "
            f"{graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges"
        )

    events = load_events(args.events, default_repo=state.repo)
    log = EventLog(log_path)

    applied = 0
    for event in events:
        delta = apply_event(state, event, extractor)
        applied += int(delta.applied)
        if not args.quiet:
            print_delta(delta)
        if delta.applied and not args.no_write:
            log.append(event)

    graph = project_graph(state)
    print(
        f"\nReplayed {len(events)} deliveries ({applied} applied, {len(events) - applied} skipped): "
        f"{graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges, {len(state.facts)} facts "
        f"({sum(1 for f in state.facts if f.valid_to)} invalidated)"
    )

    if args.verify_rebuild:
        # Default: replay the recorded extraction output, so the comparison is a
        # statement about this pipeline rather than about the model's
        # repeatability. --re-extract additionally re-runs extraction.
        fresh = rebuild(
            state,
            configured_extractor(
                rules=None if args.llm else args.rules,
                use_llm=args.llm,
            )
            if args.re_extract
            else None,
        )
        match = graph_signature(graph) == graph_signature(project_graph(fresh))

        if args.re_extract:
            label = "Extraction stability (re-ran the extractor)"
            note = "" if match else "  (a live model is not expected to reproduce itself exactly)"
        else:
            label = "Rebuild consistency (recorded extraction, deterministic layer rebuilt)"
            note = ""

        print(f"{label}: {'PASS' if match else 'FAIL'}{note}")
        if not match and not args.re_extract:
            raise SystemExit(1)

    print_opportunities(state, args.top)

    if not args.no_write:
        write_state(state_path, state)
        freshness = read_freshness(repo_storage.freshness, state.repo)
        freshness.last_state_commit_at = state.last_event_at
        freshness.semantic_status = "current"
        freshness.semantic_updated_at = state.last_event_at
        freshness.last_error = None
        write_freshness(repo_storage.freshness, freshness)
        print(f"\nWrote {state_path}")
        print(f"Wrote {log_path}")


if __name__ == "__main__":
    main()
