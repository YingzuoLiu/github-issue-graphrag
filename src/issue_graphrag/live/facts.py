from __future__ import annotations

import re

from issue_graphrag.live.models import Evidence, Fact, RepoItem
from issue_graphrag.models import TextUnit

#: ``#123`` and ``owner/repo#123`` mentions, ignoring headings and colour codes.
_TARGET = r"(?:(?P<repo>[\w.-]+/[\w.-]+))?#(?P<number>\d+)\b"
_REFERENCE = re.compile(rf"(?<![\w#]){_TARGET}")

_CLOSING = re.compile(
    rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*{_TARGET}",
    re.IGNORECASE,
)
_BLOCKED_BY = re.compile(
    rf"\b(?:blocked\s+by|depends\s+on|waiting\s+on)\b[:\s]*{_TARGET}",
    re.IGNORECASE,
)
_BLOCKS = re.compile(rf"\bblocks\b[:\s]*{_TARGET}", re.IGNORECASE)

_SNIPPET_RADIUS = 70


def snippet(text: str, start: int, end: int) -> str:
    window = text[max(0, start - _SNIPPET_RADIUS) : min(len(text), end + _SNIPPET_RADIUS)]
    return " ".join(window.split())


def _local_number(repo: str, match: re.Match[str]) -> int | None:
    qualifier = match.group("repo")
    if qualifier and qualifier.casefold() != repo.casefold():
        return None
    return int(match.group("number"))


def github_reference_numbers(text: str, repo: str) -> set[int]:
    """Local issue numbers explicitly mentioned using GitHub reference syntax."""
    return {
        number
        for match in _REFERENCE.finditer(text)
        if (number := _local_number(repo, match)) is not None
    }


def github_closing_numbers(text: str, repo: str) -> set[int]:
    """Local issue numbers preceded by a GitHub closing keyword."""
    return {
        number
        for match in _CLOSING.finditer(text)
        if (number := _local_number(repo, match)) is not None
    }


def module_of(path: str) -> str | None:
    """Top-level directory of a repository path, used as the module node."""
    cleaned = path.strip().strip("/")
    if "/" not in cleaned:
        return None
    head = cleaned.split("/")[0]
    return head or None


def file_node_name(path: str) -> str:
    """File nodes are identified by their full repo-relative path.

    Using the basename would silently merge ``src/config.py`` with
    ``tests/config.py`` into one node, which then inherits the edges of both.
    The basename is a display concern and is derived at render time.
    """
    return path.strip().strip("/")


def basename(path: str) -> str:
    return path.strip().strip("/").split("/")[-1]


def _fact(
    *,
    kind: str,
    subject: str,
    predicate: str,
    obj: str,
    document_id: str,
    moment: str,
    delivery_id: str | None,
    description: str = "",
    evidence: list[Evidence] | None = None,
    weight: float = 1.0,
) -> Fact:
    return Fact(
        kind=kind,  # type: ignore[arg-type]
        subject=subject,
        predicate=predicate,
        object=obj,
        origin="github",
        document_id=document_id,
        description=description,
        weight=weight,
        evidence=evidence or [],
        valid_from=moment,
        asserted_by=delivery_id,
    )


class ItemIndex:
    """Lookup from issue/PR number to its canonical graph node name."""

    def __init__(self, items: dict[str, RepoItem]):
        self._by_number = {item.number: item for item in items.values()}

    def node_name(self, number: int) -> str | None:
        item = self._by_number.get(number)
        return item.node_name if item else None

    def item(self, number: int) -> RepoItem | None:
        return self._by_number.get(number)

    def rename_map(self) -> dict[str, str]:
        """Map ``Issue #N`` onto ``PR #N`` for numbers that are pull requests.

        The batch normalizer canonicalizes any bare ``#N`` into ``Issue #N``.
        Once the live index knows a number is a pull request, extracted mentions
        of it must land on the same node as the deterministic PR facts.
        """
        return {
            f"Issue #{item.number}": item.node_name
            for item in self._by_number.values()
            if item.kind == "pull_request"
        }


def text_sources(item: RepoItem) -> list[tuple[str, str, str, str | None]]:
    """(kind, ref, text, url) tuples for every place this item states something."""
    sources: list[tuple[str, str, str, str | None]] = [
        ("title", item.document_id, item.title.strip(), item.url),
        ("body", item.document_id, item.body.strip(), item.url),
    ]
    for comment in item.ordered_comments():
        sources.append(("comment", f"comment-{comment.id}", comment.body.strip(), comment.url))
    return sources


def containing_unit(text_units: list[TextUnit], window: str) -> str | None:
    """Which TextUnit held this text when the fact was derived.

    Snapshotting the unit id onto the fact keeps grounding temporal. Filling a
    fact's ``source_ids`` from the document's *current* chunks instead would let
    a historical projection cite text that was written later.
    """
    probe = " ".join(window.split())
    for unit in text_units:
        if window and window in unit.text:
            return unit.id
        if probe and probe in " ".join(unit.text.split()):
            return unit.id
    return None


def _reference_facts(
    item: RepoItem,
    index: ItemIndex,
    moment: str,
    delivery_id: str | None,
    text_units: list[TextUnit],
) -> list[Fact]:
    facts: dict[tuple[str, str], Fact] = {}

    for kind, ref, text, url in text_sources(item):
        if not text:
            continue

        typed: dict[int, str] = {}
        # "Fixes #123" in an *issue* is a cross-reference, not a close: the
        # ontology says only a pull request may close an issue, and the
        # deterministic path must not emit what the schema forbids.
        for number in github_closing_numbers(text, item.repo):
            typed[number] = (
                "closes" if item.kind == "pull_request" else "references"
            )
        for match in _BLOCKED_BY.finditer(text):
            blocked_number = _local_number(item.repo, match)
            if blocked_number is not None:
                typed[blocked_number] = "blocked_by"
        blocks = {
            block_number
            for match in _BLOCKS.finditer(text)
            if (block_number := _local_number(item.repo, match)) is not None
        }

        for match in _REFERENCE.finditer(text):
            reference_number = _local_number(item.repo, match)
            if reference_number is None:
                continue
            target = index.node_name(reference_number)
            if not target or target == item.node_name:
                continue

            predicate = typed.get(reference_number, "references")
            subject, obj = item.node_name, target
            if reference_number in blocks and reference_number not in typed:
                # "A blocks #B" is stored as "#B blocked_by A" so the graph has
                # exactly one direction for the blocking relation.
                predicate, subject, obj = "blocked_by", target, item.node_name

            window = text[max(0, match.start() - _SNIPPET_RADIUS) : match.end() + _SNIPPET_RADIUS]
            evidence = Evidence(
                kind=kind,
                ref=ref,
                url=url,
                snippet=snippet(text, match.start(), match.end()),
                text_unit_id=containing_unit(text_units, window),
            )

            key = (predicate, f"{subject}->{obj}")
            if key in facts:
                facts[key].evidence.append(evidence)
                continue

            facts[key] = _fact(
                kind="relation",
                subject=subject,
                predicate=predicate,
                obj=obj,
                document_id=item.document_id,
                moment=moment,
                delivery_id=delivery_id,
                description=f"{subject} {predicate.replace('_', ' ')} {obj}",
                evidence=[evidence],
            )

    return list(facts.values())


def _file_facts(item: RepoItem, moment: str, delivery_id: str | None) -> list[Fact]:
    facts: list[Fact] = []

    for path in sorted(set(item.files)):
        node = file_node_name(path)
        if not node:
            continue

        evidence = Evidence(kind="pull_request_files", ref=path, url=item.url, snippet=path)
        file_description = f"{basename(path)} ({path})"

        facts.append(
            _fact(
                kind="entity",
                subject=node,
                predicate="is_a",
                obj="FILE",
                document_id=item.document_id,
                moment=moment,
                delivery_id=delivery_id,
                description=file_description,
                evidence=[evidence],
            )
        )
        facts.append(
            _fact(
                kind="relation",
                subject=item.node_name,
                predicate="touches",
                obj=node,
                document_id=item.document_id,
                moment=moment,
                delivery_id=delivery_id,
                description=f"{item.node_name} changes {path}",
                evidence=[evidence],
            )
        )

        module = module_of(path)
        if module:
            facts.append(
                _fact(
                    kind="entity",
                    subject=module,
                    predicate="is_a",
                    obj="MODULE",
                    document_id=item.document_id,
                    moment=moment,
                    delivery_id=delivery_id,
                    description=f"Repository module {module}",
                    evidence=[evidence],
                )
            )
            facts.append(
                _fact(
                    kind="relation",
                    subject=node,
                    predicate="belongs_to",
                    obj=module,
                    document_id=item.document_id,
                    moment=moment,
                    delivery_id=delivery_id,
                    description=f"{path} lives in {module}",
                    evidence=[evidence],
                )
            )

    return facts


def _assignee_facts(item: RepoItem, moment: str, delivery_id: str | None) -> list[Fact]:
    facts: list[Fact] = []
    for login in sorted(set(item.assignees)):
        account = f"@{login}"
        assignment_evidence = Evidence(
            kind="assignee",
            ref=item.document_id,
            url=item.url,
            snippet=f"{item.node_name} is assigned to {account}",
        )
        account_evidence = Evidence(
            kind="github_account",
            ref=login,
            url=f"https://github.com/{login}",
            snippet=account,
        )
        facts.extend(
            [
                _fact(
                    kind="entity",
                    subject=account,
                    predicate="is_a",
                    obj="CONTRIBUTOR",
                    document_id=item.document_id,
                    moment=moment,
                    delivery_id=delivery_id,
                    description=f"GitHub account {account}",
                    evidence=[account_evidence],
                ),
                _fact(
                    kind="relation",
                    subject=item.node_name,
                    predicate="assigned_to",
                    obj=account,
                    document_id=item.document_id,
                    moment=moment,
                    delivery_id=delivery_id,
                    description=f"{item.node_name} is assigned to {account}",
                    evidence=[assignment_evidence],
                ),
            ]
        )
    return facts


def github_facts_for_item(
    item: RepoItem,
    index: ItemIndex,
    moment: str,
    delivery_id: str | None = None,
    text_units: list[TextUnit] | None = None,
) -> list[Fact]:
    """Derive every fact GitHub states outright for one issue or pull request.

    Nothing here is inferred. State, labels, assignees, explicit references,
    closing keywords and changed files all come straight from the payload, so
    an LLM can never overwrite what GitHub already told us.
    """
    self_evidence = Evidence(
        kind="item",
        ref=item.document_id,
        url=item.url,
        snippet=item.document_title,
    )

    node_type = "ISSUE" if item.kind == "issue" else "PULL_REQUEST"
    facts: list[Fact] = [
        _fact(
            kind="entity",
            subject=item.node_name,
            predicate="is_a",
            obj=node_type,
            document_id=item.document_id,
            moment=moment,
            delivery_id=delivery_id,
            description=item.title,
            evidence=[self_evidence],
        ),
        _fact(
            kind="entity",
            subject=item.node_name,
            predicate="has_state",
            obj=item.lifecycle_state(),
            document_id=item.document_id,
            moment=moment,
            delivery_id=delivery_id,
            description=f"{item.node_name} is {item.lifecycle_state()}",
            evidence=[self_evidence],
        ),
    ]

    for label in item.labels:
        facts.append(
            _fact(
                kind="entity",
                subject=item.node_name,
                predicate="has_label",
                obj=label,
                document_id=item.document_id,
                moment=moment,
                delivery_id=delivery_id,
                description=f"{item.node_name} is labeled {label}",
                evidence=[self_evidence],
            )
        )

    facts.extend(_reference_facts(item, index, moment, delivery_id, text_units or []))
    facts.extend(_file_facts(item, moment, delivery_id))
    facts.extend(_assignee_facts(item, moment, delivery_id))
    return facts
