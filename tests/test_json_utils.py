import json

import pytest

from issue_graphrag.llm.json_utils import extract_json_object


def test_extract_json_object_from_pure_json():
    assert extract_json_object('{"entities": [], "relationships": []}') == {
        "entities": [],
        "relationships": [],
    }


def test_extract_json_object_from_fenced_json():
    raw = """```json
{"entities": [{"name": "Graph RAG"}], "relationships": []}
```"""

    parsed = extract_json_object(raw)

    assert parsed["entities"][0]["name"] == "Graph RAG"


def test_extract_json_object_from_surrounding_text():
    raw = "Here is the extraction result: {\"entities\": [], \"relationships\": []} Thanks."

    assert extract_json_object(raw) == {"entities": [], "relationships": []}


def test_extract_json_object_rejects_invalid_output():
    with pytest.raises(json.JSONDecodeError):
        extract_json_object("no structured json here")
