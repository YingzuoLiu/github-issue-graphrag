"""A fresh clone has no batch index, and reading it anyway is a traceback.

``read_graph`` raises ``FileNotFoundError``, which is the wrong thing to put in
front of someone who has simply not run ``build_index.py`` yet -- especially
since building it needs a provider key while the live demo does not.
"""

from __future__ import annotations

import json

from issue_graphrag.storage.json_store import BATCH_INDEX_FILES, missing_batch_index


def write(directory, name: str) -> None:
    (directory / name).write_text(json.dumps({}), encoding="utf-8")


def test_a_fresh_directory_reports_every_file_in_load_order(tmp_path):
    assert missing_batch_index(tmp_path) == list(BATCH_INDEX_FILES)


def test_a_complete_index_reports_nothing(tmp_path):
    for name in BATCH_INDEX_FILES:
        write(tmp_path, name)

    assert missing_batch_index(tmp_path) == []


def test_a_partial_index_reports_only_what_is_absent(tmp_path):
    write(tmp_path, "graph.json")

    assert missing_batch_index(tmp_path) == ["text_units.json", "community_reports.json"]


def test_a_missing_directory_is_not_an_error(tmp_path):
    assert missing_batch_index(tmp_path / "never-created") == list(BATCH_INDEX_FILES)
