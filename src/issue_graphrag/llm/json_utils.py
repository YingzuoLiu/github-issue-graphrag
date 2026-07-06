from __future__ import annotations

import json
import re
from typing import Any


_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def extract_json_object(raw: str) -> Any:
    """Parse JSON from common LLM response formats.

    Handles:
    - pure JSON
    - markdown fenced JSON blocks
    - surrounding explanatory text with a JSON object inside
    """
    text = raw.strip()
    if not text:
        raise json.JSONDecodeError("empty response", raw, 0)

    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])

    raise json.JSONDecodeError("no JSON object found", raw, 0)
