from __future__ import annotations

from issue_graphrag.chunker import documents_to_text_units
from issue_graphrag.live.models import LiveState, RepoItem
from issue_graphrag.models import SourceDocument, TextUnit

LIVE_CHUNK_MAX_CHARS = 2500
LIVE_CHUNK_OVERLAP = 250


def to_source_document(item: RepoItem) -> SourceDocument:
    """Render one issue or pull request as a single grounding document."""
    return SourceDocument(
        id=item.document_id,
        title=item.document_title,
        text=item.document_text(),
        source_type=f"github_{item.kind}",
        url=item.url,
        metadata={
            "repo": item.repo,
            "number": item.number,
            "kind": item.kind,
            "state": item.lifecycle_state(),
            "labels": item.labels,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "comment_count": len(item.comments),
        },
    )


def text_units_for(item: RepoItem) -> list[TextUnit]:
    return documents_to_text_units(
        [to_source_document(item)],
        max_chars=LIVE_CHUNK_MAX_CHARS,
        overlap=LIVE_CHUNK_OVERLAP,
    )


def all_text_units(state: LiveState) -> list[TextUnit]:
    units: list[TextUnit] = []
    for document_id in sorted(state.items):
        units.extend(text_units_for(state.items[document_id]))
    return units
