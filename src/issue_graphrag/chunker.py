from __future__ import annotations

import hashlib

from issue_graphrag.models import SourceDocument, TextUnit


def stable_id(*parts: str) -> str:
    raw = "::".join(parts).encode("utf-8")
    return hashlib.md5(raw).hexdigest()


def chunk_text(text: str, max_chars: int = 2500, overlap: int = 250) -> list[str]:
    """Split text into overlapping character chunks.

    This is intentionally simple for the MVP. Later we can replace it with a
    token-aware splitter.
    """
    clean = text.strip()
    if not clean:
        return []
    if max_chars <= overlap:
        raise ValueError("max_chars must be greater than overlap")

    chunks: list[str] = []
    start = 0
    while start < len(clean):
        end = min(start + max_chars, len(clean))
        chunks.append(clean[start:end].strip())
        if end == len(clean):
            break
        start = end - overlap
    return [c for c in chunks if c]


def documents_to_text_units(
    documents: list[SourceDocument],
    max_chars: int = 2500,
    overlap: int = 250,
) -> list[TextUnit]:
    text_units: list[TextUnit] = []
    for document in documents:
        for idx, chunk in enumerate(chunk_text(document.text, max_chars=max_chars, overlap=overlap)):
            text_units.append(
                TextUnit(
                    id=stable_id(document.id, str(idx), chunk[:80]),
                    document_id=document.id,
                    text=chunk,
                    order=idx,
                    metadata={
                        **document.metadata,
                        "document_title": document.title,
                        "source_type": document.source_type,
                        "url": document.url,
                    },
                )
            )
    return text_units
