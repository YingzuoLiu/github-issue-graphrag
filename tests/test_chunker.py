from issue_graphrag.chunker import chunk_text


def test_chunk_text_short_text():
    assert chunk_text("hello world", max_chars=100, overlap=10) == ["hello world"]


def test_chunk_text_overlap():
    chunks = chunk_text("abcdefghijklmnopqrstuvwxyz", max_chars=10, overlap=2)
    assert len(chunks) > 1
    assert chunks[0] == "abcdefghij"
    assert chunks[1].startswith("ij")
