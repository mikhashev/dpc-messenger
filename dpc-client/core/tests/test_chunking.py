"""Tests for text chunking (ADR-010, MEM-3.3)."""

from dpc_client_core.dpc_agent.chunking import Chunk, chunk_text, batched_chunks, DEFAULT_BATCH_SIZE


def test_short_text_single_chunk():
    chunks = chunk_text("Short text", source_file="a.md")
    assert len(chunks) == 1
    assert chunks[0].text == "Short text"
    assert chunks[0].source_file == "a.md"
    assert chunks[0].chunk_index == 0


def test_empty_text():
    assert chunk_text("") == []


def test_long_text_splits():
    text = "word " * 1000  # ~5000 chars
    chunks = chunk_text(text, max_chars=500, overlap_chars=50)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.text) <= 510  # boundary search may slightly exceed


def test_overlap_exists():
    text = "A" * 3000
    chunks = chunk_text(text, max_chars=1000, overlap_chars=200)
    assert len(chunks) >= 3
    assert chunks[1].char_start < chunks[0].char_end


def test_chunk_metadata():
    text = "Hello world " * 100
    chunks = chunk_text(text, source_file="test.md", max_chars=200, overlap_chars=50)
    for i, c in enumerate(chunks):
        assert c.chunk_index == i
        assert c.source_file == "test.md"
        assert c.char_start >= 0
        assert c.char_end > c.char_start


def test_batched_chunks_yields_correct_sizes():
    chunks = [Chunk(text=f"c{i}", source_file="", chunk_index=i, char_start=0, char_end=1) for i in range(10)]
    batches = list(batched_chunks(chunks, batch_size=3))
    assert len(batches) == 4  # 3+3+3+1
    assert len(batches[-1]) == 1


def test_default_batch_size():
    assert DEFAULT_BATCH_SIZE == 32
