from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    """Parse a GitHub-style ISO timestamp into an aware UTC datetime."""
    text = (value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_iso(value: datetime | str) -> str:
    """Render a timestamp in the canonical UTC form used across the live pipeline."""
    if isinstance(value, str):
        value = parse_iso(value)
    return value.astimezone(timezone.utc).strftime(ISO_FORMAT)


def max_iso(*values: str | None) -> str | None:
    known = [v for v in values if v]
    if not known:
        return None
    return to_iso(max(parse_iso(v) for v in known))


def next_iso(value: str) -> str:
    """Advance the logical ingestion clock at its one-second storage precision."""
    return to_iso(parse_iso(value) + timedelta(seconds=1))


def is_before_or_equal(left: str, right: str) -> bool:
    return parse_iso(left) <= parse_iso(right)


def is_after(left: str, right: str) -> bool:
    return parse_iso(left) > parse_iso(right)
