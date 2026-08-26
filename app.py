from __future__ import annotations

import secrets
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

from issue_graphrag.config import load_settings
from issue_graphrag.live.analytics import RadarAnalytics, safe_record
from issue_graphrag.live.contribution import opportunities
from issue_graphrag.live.history import timeline
from issue_graphrag.live.ontology import describe
from issue_graphrag.live.operations import validate_public_viewer
from issue_graphrag.live.projection import event_subgraph, project_graph
from issue_graphrag.live.radar import (
    RadarChange,
    RadarEvidence,
    RadarIssue,
    RadarSnapshot,
    load_radar_snapshot,
    read_event_snapshot,
)
from issue_graphrag.live.repositories import RepoRegistry, read_freshness
from issue_graphrag.live.store import read_state
from issue_graphrag.live.timeutil import now_utc, to_iso
from issue_graphrag.live.viz import legend_markdown, subgraph_dot
from issue_graphrag.llm.client import MockLLMClient, OpenAICompatibleClient
from issue_graphrag.models import CommunityReport, TextUnit
from issue_graphrag.retrieval.global_search import global_search
from issue_graphrag.retrieval.local_search import local_search
from issue_graphrag.retrieval.naive_search import naive_search
from issue_graphrag.retrieval.router import route_query
from issue_graphrag.storage.json_store import missing_batch_index, read_graph, read_json


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

NAVIGATION = ("Contribution Radar", "Timeline & graph", "Ask (local demo)")
RADAR_FILTERS = ("Ready", "Claimed", "Blocked", "Recently changed")

def render_shell_css() -> None:
    st.markdown(
        """
        <style>
        .block-container {max-width: 1160px; padding-top: 2rem; padding-bottom: 4rem;}
        .radar-hero {
            padding: 1.35rem 1.5rem;
            border: 1px solid rgba(124, 141, 181, .28);
            border-radius: 18px;
            background: linear-gradient(135deg, rgba(32, 83, 153, .16), rgba(31, 157, 122, .08));
            margin-bottom: 1.25rem;
        }
        .radar-kicker {
            font-size: .76rem;
            letter-spacing: .12em;
            text-transform: uppercase;
            font-weight: 700;
            color: #2f81f7;
        }
        .radar-hero h1 {font-size: clamp(2rem, 5vw, 3.45rem); margin: .2rem 0 .35rem;}
        .radar-hero p {font-size: 1.05rem; max-width: 760px; margin: 0; opacity: .82;}
        .radar-stats {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: .7rem;
            margin: 1rem 0 1.25rem;
        }
        .radar-stat {
            border: 1px solid rgba(124, 141, 181, .25);
            border-radius: 14px;
            padding: .85rem 1rem;
            background: rgba(124, 141, 181, .055);
        }
        .radar-stat strong {display: block; font-size: 1.65rem; line-height: 1.15;}
        .radar-stat span {font-size: .84rem; opacity: .72;}
        .radar-card-head {display: flex; align-items: center; gap: .55rem; flex-wrap: wrap;}
        .radar-card-title {font-weight: 720; font-size: 1.18rem; line-height: 1.35;}
        .radar-pill {
            display: inline-flex;
            border-radius: 999px;
            padding: .2rem .58rem;
            font-size: .76rem;
            font-weight: 720;
            letter-spacing: .02em;
        }
        .radar-pill-ready {background: rgba(35, 134, 84, .16); color: #2da66a;}
        .radar-pill-claimed {background: rgba(191, 123, 21, .17); color: #d5902d;}
        .radar-pill-blocked {background: rgba(207, 58, 72, .16); color: #de5965;}
        .radar-origin {font-size: .74rem; font-weight: 700; letter-spacing: .03em;}
        .radar-origin-github {color: #2f81f7;}
        .radar-origin-inference {color: #9b72ff;}
        @media (max-width: 700px) {
            .block-container {padding: 2.5rem .85rem 3rem;}
            .radar-hero {padding: 1.1rem; border-radius: 14px;}
            .radar-stats {grid-template-columns: repeat(2, minmax(0, 1fr));}
            .radar-stat {padding: .7rem .8rem;}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_resource
def load_processed_data():
    settings = load_settings()
    processed = settings.processed_data_dir
    graph = read_graph(processed / "graph.json")
    text_units = [TextUnit.model_validate(x) for x in read_json(processed / "text_units.json")]
    reports = [
        CommunityReport.model_validate(x)
        for x in read_json(processed / "community_reports.json")
    ]
    return graph, text_units, reports


@st.cache_data(show_spinner=False)
def load_radar_index(
    root_text: str,
    configured_repos: tuple[str, ...],
    repo: str,
    state_mtime: int,
    log_mtime: int,
    freshness_mtime: int,
    seed_mtime: int,
    inbox_token: tuple[int, int, int],
    checked_at: str,
) -> RadarSnapshot:
    del state_mtime, log_mtime, freshness_mtime, seed_mtime, inbox_token
    registry = RepoRegistry(Path(root_text), configured_repos)
    return load_radar_snapshot(registry.paths(repo), observed_at=checked_at)


@st.cache_data(show_spinner=False)
def load_inspection_index(
    repo: str,
    state_path_text: str,
    log_path_text: str,
    state_mtime: int,
    log_mtime: int,
):
    del state_mtime, log_mtime
    state = read_state(Path(state_path_text))
    if state.repo.casefold() != repo.casefold():
        raise ValueError(f"state belongs to {state.repo!r}, not {repo!r}")
    history = read_event_snapshot(
        Path(log_path_text),
        repo,
        expected_delivery_ids=state.processed_deliveries,
    )
    return state, list(history.events), history


def _mtime(path: Path) -> int:
    return path.stat().st_mtime_ns if path.exists() else 0


def _sqlite_token(path: Path) -> tuple[int, int, int]:
    return (
        _mtime(path),
        _mtime(Path(f"{path}-wal")),
        _mtime(Path(f"{path}-shm")),
    )


def _anonymous_session() -> str:
    if "_radar_anonymous_session" not in st.session_state:
        st.session_state["_radar_anonymous_session"] = secrets.token_urlsafe(18)
    return str(st.session_state["_radar_anonymous_session"])


def _track(
    event_name: str,
    repo: str,
    issue_number: int | None,
    ui_source: str,
) -> None:
    settings = load_settings()
    safe_record(
        RadarAnalytics(settings.radar_analytics_path),
        event_name=event_name,
        anonymous_session=_anonymous_session(),
        repo=repo,
        issue_number=issue_number,
        ui_source=ui_source,
    )


def _safe_github_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.netloc.casefold() == "github.com":
        return url
    return None


def make_llm():
    settings = load_settings()
    if settings.llm_provider == "mock":
        return MockLLMClient()
    if settings.llm_provider in {"openrouter", "openai-compatible"}:
        if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
            raise ValueError(
                "LLM_BASE_URL, LLM_API_KEY, and LLM_MODEL are required for the LLM provider."
            )
        return OpenAICompatibleClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            model=settings.llm_model,
        )
    raise ValueError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")


def retrieve_context(mode: str, question: str, graph, text_units, reports):
    resolved_mode = route_query(question, mode)
    if resolved_mode == "naive":
        results = naive_search(text_units, question, top_k=8)
    elif resolved_mode == "local":
        results = local_search(graph, reports, text_units, question, top_k=8)
    elif resolved_mode == "global":
        results = global_search(reports, question, top_k=8)
    else:
        raise ValueError(f"Unsupported mode: {resolved_mode}")
    context = "\n\n".join(
        f"### {result.id} | score={result.score:.3f}\n\n{result.text}" for result in results
    )
    return resolved_mode, context


def render_ask_page(mode: str, generate_answer: bool, show_context: bool, question: str) -> None:
    st.title("Ask · local demo")
    st.caption(
        "A secondary developer surface for the batch GraphRAG index. "
        "The public MVP starts with the deterministic Contribution Radar."
    )
    missing = missing_batch_index(load_settings().processed_data_dir)
    if missing:
        st.warning(
            "No batch index yet — `"
            + "`, `".join(missing)
            + "` "
            + ("is" if len(missing) == 1 else "are")
            + " missing. Build one with:\n\n"
            "```bash\n"
            "python scripts/fetch_github_issues.py trustgraph-ai/trustgraph --state open --limit 20\n"
            "python scripts/build_index.py trustgraph-ai__trustgraph_issues.json\n"
            "```\n\n"
            "This action can call the configured LLM provider. Radar browsing does not."
        )
        return
    if not st.button("Run query", type="primary"):
        st.info("Choose a demo question or enter your own, then click Run query.")
        return
    graph, text_units, reports = load_processed_data()
    with st.spinner("Retrieving GraphRAG context..."):
        resolved_mode, context = retrieve_context(mode, question, graph, text_units, reports)
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


def _render_hero() -> None:
    st.markdown(
        """
        <section class="radar-hero">
          <div class="radar-kicker">Read-only contribution intelligence</div>
          <h1>Find contribution opportunities with evidence you can inspect.</h1>
          <p>
            Contribution Radar combines GitHub-stated status, assignees, pull requests and
            blockers with clearly labeled inferred context—then shows the evidence behind
            every recommendation.
          </p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def _render_freshness(snapshot: RadarSnapshot) -> None:
    freshness = snapshot.freshness
    if freshness.source_status == "stale":
        detail = freshness.source_error or "the latest source refresh did not complete"
        st.warning(
            "Showing the last-good GitHub facts, not a confirmed current snapshot. "
            f"Reason: {detail}. Next attempt: {freshness.next_source_sync_at or 'not scheduled'}."
        )
    elif freshness.source_status == "not_started":
        st.info(
            "Initial source sync has not been confirmed. Results below are a local last-known "
            "snapshot and may be incomplete."
        )
    elif snapshot.source_schedule_overdue:
        st.warning(
            "The scheduled source refresh is overdue. Showing last-known GitHub facts, not "
            f"a confirmed current snapshot. Last successful refresh: "
            f"{freshness.last_source_sync_at or 'not recorded'}; expected next refresh: "
            f"{freshness.next_source_sync_at or 'not recorded'}."
        )
    elif snapshot.source_current_is_confirmed:
        source = "scheduled sync" if freshness.source_kind == "scheduled_sync" else "bootstrap"
        st.success(
            f"GitHub facts current via {source} · last successful sync "
            f"{freshness.last_source_sync_at}"
        )
    elif snapshot.source_observation_current_is_confirmed and snapshot.inbox.status == "pending":
        st.warning(
            "GitHub changes are pending deterministic processing. The source refresh succeeded "
            f"at {freshness.last_source_sync_at}, but cards remain a last-known projection until "
            f"{snapshot.inbox.active_deliveries} queued delivery or deliveries are committed."
        )
    elif snapshot.source_observation_current_is_confirmed and snapshot.inbox.status == "degraded":
        st.warning(
            "GitHub delivery processing is degraded. Showing last-known facts; failed queue work "
            "must be recovered before projection currentness can be confirmed."
        )
    elif snapshot.source_observation_current_is_confirmed and snapshot.inbox.status in {
        "unknown",
        "unavailable",
    }:
        st.warning(
            f"{snapshot.inbox.message or 'Inbox state is unavailable.'} Treat cards as "
            "last-known facts, not a confirmed current projection."
        )
    elif snapshot.source_observation_current_is_confirmed:
        st.warning(
            "GitHub source refresh succeeded, but the state commit time is missing. Treat cards "
            "as a degraded last-known projection, not current results."
        )
    else:
        st.warning(
            "GitHub freshness cannot be confirmed because the successful-sync time or source "
            "kind is missing. Treat these as degraded last-known facts, not current results."
        )

    if not snapshot.source_observation_current_is_confirmed:
        if snapshot.inbox.status == "pending":
            st.warning(
                "GitHub changes are also pending deterministic processing. Cards remain the "
                f"last-known committed state while {snapshot.inbox.active_deliveries} delivery "
                "or deliveries wait in this repository's inbox."
            )
        elif snapshot.inbox.status == "degraded":
            st.warning(
                "Deterministic delivery processing is also degraded for this repository; no "
                "failed or pending work has been presented as current."
            )
        elif snapshot.inbox.status == "unavailable":
            st.warning(snapshot.inbox.message or "Inbox health is unavailable for this repository.")

    if freshness.semantic_status == "degraded":
        st.warning(
            "GitHub status remains available. Inferred context is delayed; any last-good "
            "inference is labeled separately and must not be read as current GitHub fact."
        )
    elif freshness.semantic_status in {"pending", "not_started"}:
        st.info(
            "GitHub facts are usable while semantic enrichment is pending. "
            "Inference does not decide Ready, Claimed or Blocked."
        )
    elif snapshot.inbox.active_semantic_jobs:
        st.info(
            f"GitHub facts remain available while {snapshot.inbox.active_semantic_jobs} semantic "
            "job or jobs are pending. Inferred context is last-good until they finish."
        )
    elif not snapshot.semantic_current_is_confirmed:
        st.warning(
            "Semantic freshness cannot be confirmed against the selected repository state. "
            "Inferred context is degraded and remains separate from GitHub facts."
        )

    if snapshot.coverage.status == "bounded" and not snapshot.coverage.cap_reached:
        st.caption(snapshot.coverage.message)
    else:
        st.warning(snapshot.coverage.message)

    if snapshot.history_status in {"partial", "unavailable"}:
        st.warning(snapshot.history_message or "Recent-change history is temporarily unavailable.")


def _render_counts(snapshot: RadarSnapshot) -> None:
    ready_label = "Ready now" if snapshot.source_current_is_confirmed else "Last-known Ready"
    counts = (
        (snapshot.count("available"), ready_label),
        (snapshot.count("claimed"), "Already claimed"),
        (snapshot.count("blocked"), "Blocked"),
        (len(snapshot.recent_changes), "Recently changed"),
    )
    cells = "".join(
        f'<div class="radar-stat"><strong>{count}</strong><span>{escape(label)}</span></div>'
        for count, label in counts
    )
    st.markdown(f'<div class="radar-stats">{cells}</div>', unsafe_allow_html=True)


def _origin_badge(origin: str) -> str:
    if origin == "github":
        return '<span class="radar-origin radar-origin-github">GITHUB FACT</span>'
    return '<span class="radar-origin radar-origin-inference">INFERRED CONTEXT</span>'


def _status_class(issue: RadarIssue) -> str:
    return {
        "available": "ready",
        "claimed": "claimed",
        "blocked": "blocked",
    }.get(issue.status, "blocked")


def _show_issue(issue: RadarIssue, repo: str, source: str) -> None:
    st.session_state["_radar_selected_issue"] = issue.number
    _track("opportunity_opened", repo, issue.number, source)


def _render_github_action(
    label: str,
    url: str,
    *,
    repo: str,
    issue_number: int,
    source: str,
    key: str,
    primary: bool = False,
    use_container_width: bool = False,
) -> None:
    """Bind analytics to the actual outbound link activation, never link reveal."""
    st.link_button(
        label,
        url,
        key=key,
        type="primary" if primary else "secondary",
        on_click=_track,
        args=("github_opened", repo, issue_number, source),
        use_container_width=use_container_width,
    )


def _render_issue_card(issue: RadarIssue, repo: str) -> None:
    with st.container(border=True):
        st.markdown(
            '<div class="radar-card-head">'
            f'<span class="radar-pill radar-pill-{_status_class(issue)}">'
            f"{escape(issue.status_label)}</span>"
            f'<span class="radar-card-title">#{issue.number} · {escape(issue.title)}</span>'
            "</div>",
            unsafe_allow_html=True,
        )
        updated = issue.updated_at or "not recorded"
        last_related_change = (
            issue.recent_changes[0].observed_at if issue.recent_changes else updated
        )
        st.caption(
            f"Deterministic score {issue.score:.2f} · Last related change "
            f"{last_related_change}"
        )
        for reason in issue.reasons[:3]:
            st.markdown(
                f"{_origin_badge(reason.origin)} &nbsp; {escape(reason.text)}",
                unsafe_allow_html=True,
            )
        if issue.labels:
            st.caption("Labels · " + " · ".join(issue.labels[:5]))
        assignees = ", ".join(issue.assignees) or "Unassigned"
        claiming_prs = ", ".join(issue.claimed_by) or "None"
        blockers = ", ".join(issue.blocked_by) or "None"
        if issue.blocking_dependency_count:
            blockers = f"{blockers} · {issue.blocking_dependency_count} native open dependencies"
        st.markdown(
            f"{_origin_badge('github')} &nbsp; <strong>Assignees:</strong> "
            f"{escape(assignees)}<br>"
            f"{_origin_badge('github')} &nbsp; <strong>Claiming / closing PR:</strong> "
            f"{escape(claiming_prs)}<br>"
            f"{_origin_badge('github')} &nbsp; <strong>Blockers:</strong> {escape(blockers)}",
            unsafe_allow_html=True,
        )
        concepts = ", ".join(issue.concepts) or "None recorded"
        st.markdown(
            f"{_origin_badge('inference')} &nbsp; "
            f"<strong>Related technical concepts:</strong> "
            f"{escape(concepts)}",
            unsafe_allow_html=True,
        )
        if not issue.evidence_complete:
            st.warning(
                "Evidence is incomplete. Treat this status as needing source verification, "
                "not as a confirmed recommendation."
            )
        if st.button(
            f"View #{issue.number} details",
            key=f"detail__{repo.replace('/', '__')}__{issue.number}",
            use_container_width=True,
        ):
            _show_issue(issue, repo, "radar_card")
        github_url = _safe_github_url(issue.url)
        if github_url:
            _render_github_action(
                f"Open #{issue.number} on GitHub",
                github_url,
                repo=repo,
                issue_number=issue.number,
                source="radar_card",
                key=f"github_card__{repo.replace('/', '__')}__{issue.number}",
                use_container_width=True,
            )
        else:
            st.warning("GitHub original link unavailable; verify source evidence before acting.")


def _render_recent_change(change: RadarChange, snapshot: RadarSnapshot) -> None:
    before = change.before_label or "Not listed"
    after = change.after_label or "Not listed"
    with st.container(border=True):
        st.markdown(f"#### #{change.number} · {change.title}")
        st.markdown(f"**{before} → {after}** · {change.change.replace('_', ' ')}")
        st.caption(
            f"{change.source_label} · source time {change.observed_at} · indexed "
            f"{change.indexed_at}"
        )
        for reason in change.reasons[:3]:
            st.markdown(
                f"{_origin_badge(reason.origin)} &nbsp; {escape(reason.text)}",
                unsafe_allow_html=True,
            )
            if not reason.traceable:
                st.caption("No direct source URL is available for this change reason.")
        issue = snapshot.issue(change.number)
        if issue is not None and st.button(
            f"Inspect change to #{change.number}",
            key=f"change__{snapshot.repo.replace('/', '__')}__{change.number}",
            use_container_width=True,
        ):
            _show_issue(issue, snapshot.repo, "recently_changed")


def _render_empty_filter(filter_name: str, snapshot: RadarSnapshot) -> None:
    if filter_name == "Ready":
        if snapshot.source_current_is_confirmed:
            st.info(
                "No issue is currently confirmed Ready. Review Claimed and Blocked to understand "
                "why, or try again after the next source sync."
            )
        else:
            st.info(
                "No Ready issue appears in this last-known snapshot. Restore current source and "
                "queue health before treating the result as current."
            )
    elif filter_name == "Recently changed" and snapshot.history_status != "current":
        st.warning(
            snapshot.history_message
            or "Recent-change history is unavailable; current GitHub facts are still shown."
        )
    else:
        st.info(f"No {filter_name.lower()} issues are present in this bounded snapshot.")


def _render_evidence_row(row: RadarEvidence) -> None:
    label = escape(row.label or row.ref or "Source")
    url = _safe_github_url(row.url)
    if url:
        st.markdown(f"- [{label}]({url})")
    else:
        st.markdown(f"- {label} · source URL unavailable")
    if row.snippet:
        st.caption(f"“{row.snippet[:260]}”")


def _toggle_issue_set(key: str, number: int) -> bool:
    visible = set(st.session_state.get(key, set()))
    if number in visible:
        visible.remove(number)
        enabled = False
    else:
        visible.add(number)
        enabled = True
    st.session_state[key] = visible
    return enabled


def _render_issue_detail(issue: RadarIssue, snapshot: RadarSnapshot) -> None:
    st.divider()
    left, right = st.columns([5, 1])
    with left:
        st.markdown(f"## #{issue.number} · {issue.title}")
        st.caption(
            f"{issue.status_label} · deterministic score {issue.score:.2f} · "
            f"GitHub updated {issue.updated_at or 'not recorded'}"
        )
    with right:
        if st.button("Close", key=f"close_detail__{issue.number}", use_container_width=True):
            st.session_state.pop("_radar_selected_issue", None)
            st.rerun()

    st.markdown("### Why this recommendation")
    for reason in issue.reasons:
        st.markdown(
            f"{_origin_badge(reason.origin)} &nbsp; {escape(reason.text)}",
            unsafe_allow_html=True,
        )
        if not reason.traceable:
            st.caption("No direct source URL is available for this reason.")

    st.markdown("### GitHub-stated facts")
    st.caption("These fields come from GitHub payloads or deterministic GitHub syntax parsing.")
    for fact_row in issue.github_facts:
        st.markdown(f"**{fact_row.label}:** {fact_row.value}")

    st.markdown("### Inferred context")
    if not snapshot.semantic_current_is_confirmed:
        st.warning(
            f"Semantic status is {snapshot.freshness.semantic_status}. "
            "The following is last-good or pending inferred context, not a current GitHub fact."
        )
    if not issue.inferred_context:
        st.caption("No schema-validated inferred context is currently available for this issue.")
    for inferred in issue.inferred_context[:12]:
        st.markdown(
            f"{_origin_badge('inference')} &nbsp; {escape(inferred.description)} "
            f"`{inferred.relation}`",
            unsafe_allow_html=True,
        )

    if issue.pull_requests:
        st.markdown("### Related pull requests and files")
        for pull in issue.pull_requests:
            target = _safe_github_url(pull.url)
            heading = f"PR #{pull.number} · {pull.title} · {pull.state}"
            st.markdown(f"**[{heading}]({target})**" if target else f"**{heading}**")
            if pull.files:
                st.caption("Changed files · " + " · ".join(pull.files[:12]))

    evidence_visible = issue.number in set(
        st.session_state.get("_radar_evidence_visible", set())
    )
    evidence_label = "Hide source evidence" if evidence_visible else "Show source evidence"
    if st.button(
        evidence_label,
        key=f"evidence_toggle__{snapshot.repo.replace('/', '__')}__{issue.number}",
    ):
        if _toggle_issue_set("_radar_evidence_visible", issue.number):
            _track("evidence_opened", snapshot.repo, issue.number, "issue_detail")
        st.rerun()

    if evidence_visible:
        st.markdown("#### GitHub evidence")
        if not issue.github_evidence:
            st.warning("No GitHub evidence URL is available; this recommendation needs verification.")
        for github_evidence in issue.github_evidence:
            _render_evidence_row(github_evidence)
        st.markdown("#### Evidence for inferred context")
        if not issue.inferred_evidence:
            st.caption("No inferred evidence is currently available.")
        for inferred_evidence in issue.inferred_evidence:
            _render_evidence_row(inferred_evidence)

    if issue.recent_changes:
        st.markdown("### Recommendation change history")
        for change in issue.recent_changes:
            st.markdown(
                f"- {change.before_label or 'Not listed'} → "
                f"{change.after_label or 'Not listed'} at {change.indexed_at} "
                f"({change.source_label})"
            )

    github_url = _safe_github_url(issue.url)
    if github_url:
        st.caption("The following action opens the GitHub source of truth in a new tab.")
        _render_github_action(
            "Open on GitHub",
            github_url,
            repo=snapshot.repo,
            issue_number=issue.number,
            source="issue_detail",
            key=f"github_toggle__{snapshot.repo.replace('/', '__')}__{issue.number}",
            primary=True,
        )
    else:
        st.warning("The GitHub source URL is unavailable, so no outbound action is offered.")


def _reset_repo_navigation(repo: str) -> bool:
    previous = st.session_state.get("_radar_active_repo")
    if previous == repo:
        return False
    st.session_state["_radar_active_repo"] = repo
    st.session_state["radar_filter"] = "Ready"
    st.session_state.pop("_radar_selected_issue", None)
    st.session_state["_radar_evidence_visible"] = set()
    return True


def _record_radar_view(repo: str) -> None:
    viewed = set(st.session_state.get("_radar_viewed_repos", set()))
    if repo in viewed:
        return
    _track("radar_viewed", repo, None, "radar_page")
    viewed.add(repo)
    st.session_state["_radar_viewed_repos"] = viewed


def render_radar_page() -> None:
    _render_hero()
    settings = load_settings()
    try:
        registry = RepoRegistry(settings.repo_data_dir, settings.github_repos)
        repos = registry.repositories()
    except Exception as exc:
        st.error(f"Repository configuration could not be read ({type(exc).__name__}).")
        st.info("Check `GITHUB_REPOS` or the repository registry, then reload the page.")
        return
    if not repos:
        st.info(
            "No repositories are configured yet. Set `GITHUB_REPOS` and complete an initial "
            "backfill before sharing this page."
        )
        return

    selected_repo = st.selectbox(
        "Repository",
        repos,
        key="radar_repository",
        help="Only operator-configured repositories can be selected.",
    )
    if _reset_repo_navigation(selected_repo):
        _track("repo_selected", selected_repo, None, "repo_selector")
    _record_radar_view(selected_repo)

    try:
        paths = registry.paths(selected_repo)
    except Exception as exc:
        st.error(f"Repository configuration could not be read ({type(exc).__name__}).")
        st.info("Check `GITHUB_REPOS` or the repository registry, then reload the page.")
        return
    if not paths.state.exists():
        st.info(
            "Loading initial repository index… No ranking is shown until an initial "
            "state snapshot has been durably committed."
        )
        st.code(
            f"python scripts/fetch_live_seed.py {selected_repo}\n"
            "python scripts/replay_events.py --verify-rebuild",
            language="bash",
        )
        return

    try:
        with st.spinner("Loading repository data…"):
            snapshot = load_radar_index(
                str(settings.repo_data_dir),
                settings.github_repos,
                selected_repo,
                _mtime(paths.state),
                _mtime(paths.event_log),
                _mtime(paths.freshness),
                _mtime(paths.bootstrap_seed),
                _sqlite_token(paths.inbox),
                to_iso(now_utc().replace(second=0, microsecond=0)),
            )
    except Exception as exc:
        st.error(
            f"This repository could not be loaded safely ({type(exc).__name__}). "
            "No data from another repository has been substituted."
        )
        if st.button("Retry repository load", type="primary"):
            load_radar_index.clear()
            st.rerun()
        return

    _render_freshness(snapshot)
    _render_counts(snapshot)
    filter_name = st.radio(
        "Opportunity view",
        RADAR_FILTERS,
        horizontal=True,
        key="radar_filter",
    )
    previous_filter = st.session_state.get("_radar_active_filter")
    if previous_filter != filter_name:
        st.session_state["_radar_active_filter"] = filter_name
        st.session_state.pop("_radar_selected_issue", None)

    if filter_name == "Recently changed":
        st.caption(
            "Latest recorded status or score change per issue (up to 12), ordered from the "
            "complete event history available for this repository."
        )
        if not snapshot.recent_changes:
            _render_empty_filter(filter_name, snapshot)
        for change in snapshot.recent_changes:
            _render_recent_change(change, snapshot)
    else:
        status = {"Ready": "available", "Claimed": "claimed", "Blocked": "blocked"}[filter_name]
        selected = [issue for issue in snapshot.opportunities if issue.status == status]
        if not selected:
            _render_empty_filter(filter_name, snapshot)
        for issue in selected:
            _render_issue_card(issue, snapshot.repo)

    selected_number = st.session_state.get("_radar_selected_issue")
    if isinstance(selected_number, int):
        selected_issue = snapshot.issue(selected_number)
        if selected_issue is not None:
            _render_issue_detail(selected_issue, snapshot)


def render_opportunities_table(graph, caption: str) -> None:
    ranked = opportunities(graph)
    st.subheader("Recommendation snapshot")
    st.caption(caption)
    if not ranked:
        st.info("No open contribution opportunities at this point in the timeline.")
        return
    st.dataframe(
        [
            {
                "issue": item.node,
                "score": item.score,
                "status": "Ready" if item.status == "available" else item.status.title(),
                "title": item.title,
                "concepts": ", ".join(item.concepts),
                "assignees": ", ".join(item.assignees),
                "claimed by": ", ".join(item.claimed_by),
                "blocked by": ", ".join(item.blocked_by),
            }
            for item in ranked
        ],
        hide_index=True,
        use_container_width=True,
    )


def render_change_log(delta) -> None:
    added = delta.changes_of("added")
    updated = delta.changes_of("updated")
    invalidated = delta.changes_of("invalidated")
    superseded = delta.changes_of("superseded")
    left, right = st.columns(2)
    with left:
        st.markdown(f"**New facts ({len(added)})**")
        for fact in added:
            st.markdown(f"- `{fact.origin}` {fact.label()}")
        if not added:
            st.caption("none")
        if updated:
            st.markdown(f"**Re-asserted with new evidence ({len(updated)})**")
            for fact in updated:
                st.markdown(f"- `{fact.origin}` {fact.label()}")
    with right:
        st.markdown(f"**Invalidated facts ({len(invalidated)})**")
        for fact in invalidated:
            st.markdown(f"- `{fact.origin}` {fact.label()}")
        if not invalidated:
            st.caption("none")
        if superseded:
            st.markdown(f"**Superseded versions ({len(superseded)})**")
            st.caption("Still true, but the evidence moved to a new immutable fact version.")
    if delta.opportunity_changes:
        st.markdown("**Recommendation changes**")
        for change in delta.opportunity_changes:
            before = f"{change.before_status} / {change.before_score}" if change.before_status else "—"
            after = f"{change.after_status} / {change.after_score}" if change.after_status else "—"
            st.markdown(f"- **{change.node}** {change.change}: {before} → {after}")
            for reason in change.reasons:
                st.caption(f"because {reason}")


def render_edge_evidence(graph, delta) -> None:
    edges = sorted(
        {
            (str(source), str(target))
            for source, target in graph.edges
            if graph.edges[source, target].get("change") in ("added", "invalidated")
        }
    )
    if not edges:
        st.caption("This event changed no edges.")
        return
    label = st.selectbox(
        "Inspect the evidence behind an edge",
        [f"{source} — {target}" for source, target in edges],
    )
    source, target = edges[[f"{s} — {t}" for s, t in edges].index(label)]
    data = graph.edges[source, target]
    for row in data.get("directed_relations", []):
        origin = "stated by GitHub" if row["origin"] == "github" else "inferred"
        st.markdown(f"**{row['source']} --{row['relation']}--> {row['target']}** ({origin})")
    for item in data.get("evidence", []):
        url = _safe_github_url(item.get("url"))
        ref = f"[{item['ref']}]({url})" if url else item["ref"]
        st.markdown(f"- `{item['kind']}` {ref}")
        if item.get("snippet"):
            st.caption(f"“{item['snippet']}”")


def render_inspection_page() -> None:
    st.title("Timeline & graph")
    st.caption(
        "Secondary inspection tools for auditing how one delivery changed facts and the "
        "recommendation graph. The Radar remains the primary product surface."
    )
    settings = load_settings()
    try:
        registry = RepoRegistry(settings.repo_data_dir, settings.github_repos)
        repos = registry.repositories()
    except Exception as exc:
        st.error(f"Repository configuration could not be read ({type(exc).__name__}).")
        st.info("Check `GITHUB_REPOS` or the repository registry, then reload the page.")
        return
    if not repos:
        st.info("No configured repository is available for inspection.")
        return
    selected_repo = st.selectbox("Repository", repos, key="inspection_repository")
    try:
        paths = registry.paths(selected_repo)
    except Exception as exc:
        st.error(f"Repository configuration could not be read ({type(exc).__name__}).")
        st.info("Check `GITHUB_REPOS` or the repository registry, then reload the page.")
        return
    if not paths.state.exists():
        st.info("Initial index is still in progress; there is no timeline to inspect yet.")
        return
    try:
        state, events, history = load_inspection_index(
            selected_repo,
            str(paths.state),
            str(paths.event_log),
            _mtime(paths.state),
            _mtime(paths.event_log),
        )
    except Exception as exc:
        st.error(f"Inspection data could not be loaded ({type(exc).__name__}).")
        return
    freshness = read_freshness(paths.freshness, state.repo)
    st.caption(
        f"Source `{freshness.source_status}` · semantic `{freshness.semantic_status}` · "
        f"last state commit `{freshness.last_state_commit_at or 'not recorded'}`"
    )
    if history.status in {"partial", "unavailable"}:
        st.warning(history.message)
    views = timeline(state, events)
    current = project_graph(state)
    if not views:
        st.info("The index is present, but no complete event history is available yet.")
        render_opportunities_table(current, "Current deterministic projection.")
        return
    options = ["Now (all events applied)"] + [
        f"{index + 1}. {view.event.summary()} · {view.event.observation_label()} "
        f"@ {view.after_moment}"
        for index, view in enumerate(views)
    ]
    choice = st.select_slider("Point in the event timeline", options=options, value=options[0])
    if choice == options[0]:
        render_opportunities_table(current, "Every complete delivery applied.")
    else:
        view = views[options.index(choice) - 1]
        event = view.event
        st.markdown(
            f"**Delivery `{event.delivery_id}` — {event.summary()}** indexed at "
            f"{view.after_moment}  \n{event.observation_label()}. "
            f"GitHub source time: `{event.received_at}`."
        )
        hops = st.slider("Neighbourhood hops", min_value=1, max_value=3, value=1)
        subgraph = event_subgraph(state, view.delta, hops=hops, moment=view.after_moment)
        st.graphviz_chart(subgraph_dot(subgraph, title=event.summary()))
        st.caption(legend_markdown())
        render_change_log(view.delta)
        with st.expander("Evidence for the edges this event touched", expanded=False):
            render_edge_evidence(subgraph, view.delta)
        render_opportunities_table(
            project_graph(state, view.after_moment),
            f"Ranking as it stood at {view.after_moment}.",
        )
    with st.expander("Graph ontology", expanded=False):
        schema = describe()
        st.markdown("**Node types**")
        for node_type in schema["node_types"]:
            st.caption(
                f"`{node_type['name']}` ({node_type['origin']}) — "
                f"{node_type['description']}"
            )
        st.markdown("**Predicates**")
        for predicate in schema["predicates"]:
            st.caption(
                f"`{predicate['name']}` ({predicate['origin']}) — "
                f"{predicate['description']}"
            )


def main() -> None:
    settings = load_settings()
    validate_public_viewer(settings)
    st.set_page_config(page_title="Contribution Radar", page_icon="🎯", layout="wide")
    render_shell_css()
    with st.sidebar:
        st.markdown("### Contribution Radar")
        navigation = NAVIGATION[:1] if settings.public_radar_only else NAVIGATION
        page = st.radio("Navigate", navigation, label_visibility="collapsed")
        st.caption("Public browsing is read-only. GitHub remains the source of truth.")
        if page == "Ask (local demo)":
            st.divider()
            mode = st.selectbox("Retrieval mode", ["auto", "local", "global", "naive"])
            generate_answer = st.checkbox("Generate grounded answer", value=True)
            show_context = st.checkbox("Show retrieved context", value=True)
            demo = st.radio(
                "Demo question",
                [
                    "Why is graph-rag slow and which components are involved?",
                    "How can TrustGraph improve document retrieval with hybrid retrieval?",
                    "What is the Kafka backend issue about?",
                    "What are the main technical contribution opportunities in this repo?",
                ],
            )
        else:
            mode, generate_answer, show_context, demo = "auto", False, False, ""

    if page == "Contribution Radar":
        render_radar_page()
    elif page == "Timeline & graph":
        render_inspection_page()
    else:
        question = st.text_area("Question", value=demo, height=100)
        render_ask_page(mode, generate_answer, show_context, question)


if __name__ == "__main__":
    main()
