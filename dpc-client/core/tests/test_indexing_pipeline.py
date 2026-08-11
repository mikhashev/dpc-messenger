"""Tests for incremental indexing pipeline (ADR-010, MEM-3.7, ADR-024 Phase 1.6b.1)."""

import numpy as np
from unittest.mock import MagicMock

from dpc_client_core.dpc_agent.indexing_pipeline import (
    index_single_file, full_rebuild, should_index,
)


def _mock_embedding_provider(dims=4):
    provider = MagicMock()
    provider.embed_batch.return_value = [[0.1] * dims]
    return provider


def _mock_backend():
    """Mock RetrievalBackend with vector/text/fuser attributes.

    Each component is itself a MagicMock — preserves the call assertions
    used in the original tests (`vector.add.called`, `vector.clear.called`).
    """
    backend = MagicMock()
    backend.vector = MagicMock()
    backend.text = MagicMock()
    backend.fuser = MagicMock()
    return backend


def test_index_single_file(tmp_path):
    (tmp_path / "topic.md").write_text("Some knowledge content here for testing", encoding="utf-8")
    provider = _mock_embedding_provider()
    provider.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
    backend = _mock_backend()

    count = index_single_file(tmp_path / "topic.md", provider, backend)
    assert count >= 1
    assert provider.embed.called or provider.embed_batch.called
    assert backend.vector.add.called
    assert backend.text.add.called


def test_index_binary_file_skipped(tmp_path):
    (tmp_path / "image.png").write_bytes(b"\x89PNG")
    count = index_single_file(
        tmp_path / "image.png", _mock_embedding_provider(), _mock_backend()
    )
    assert count == 0


def test_full_rebuild(tmp_path):
    (tmp_path / "a.md").write_text("Alpha content", encoding="utf-8")
    (tmp_path / "b.md").write_text("Beta content", encoding="utf-8")
    (tmp_path / "_meta.json").write_text("{}", encoding="utf-8")

    provider = _mock_embedding_provider()
    provider.embed_batch.return_value = [[0.1, 0.2, 0.3, 0.4]]
    backend = _mock_backend()

    count = full_rebuild(tmp_path, provider, backend)
    assert count >= 2
    assert backend.vector.clear.called
    assert backend.vector.add.called
    assert backend.text.add.called


def test_debounce():
    assert should_index("test.md") is True
    assert should_index("test.md") is False


def test_full_rebuild_empty(tmp_path):
    count = full_rebuild(
        tmp_path, _mock_embedding_provider(), _mock_backend()
    )
    assert count == 0


_KNOWLEDGE_COMMIT = """---
# Commit Identification
topic: how the relay reattributes an author
commit_id: commit-1d4be86bc4463f6c
content_hash: 5081ecf18571ad48

# Cryptographic Signatures
signatures:
  dpc-node-86cdcd: "DwaTgCbLc9S8Yers6YCmloY+4SVFW2lpWiMtVDejcnTr5g0qq7oz"

---

# How The Relay Reattributes An Author

## Overview

Messages passed on by a relay arrived under the relay's name.
"""


def test_a_knowledge_commit_is_headed_by_its_topic_not_its_envelope(tmp_path):
    """The envelope opens with `# Commit Identification` and is not the document.

    Read as markdown it made all 323 files in the live store carry that same
    heading and a hash as their excerpt — identical to each other and silent
    about their contents.
    """
    path = tmp_path / "relay.md"
    path.write_text(_KNOWLEDGE_COMMIT, encoding="utf-8")
    backend = _mock_backend()

    index_single_file(path, _mock_embedding_provider(), backend)

    meta = backend.vector.add.call_args[0][0][0].meta
    assert meta["heading"] == "How The Relay Reattributes An Author"
    assert meta["text"].startswith("# How The Relay Reattributes An Author")
    assert "commit_id" not in meta["text"]
    assert "signatures" not in meta["text"]

    embedded = backend.text.add.call_args[0][0][0].text
    assert "content_hash" not in embedded, "the envelope must not be embedded either"


def test_a_document_without_an_envelope_is_left_alone(tmp_path):
    path = tmp_path / "plain.md"
    path.write_text("# Plain Heading\n\nBody text.\n", encoding="utf-8")
    backend = _mock_backend()

    index_single_file(path, _mock_embedding_provider(), backend)

    meta = backend.vector.add.call_args[0][0][0].meta
    assert meta["heading"] == "Plain Heading"
    assert meta["text"].startswith("# Plain Heading")


def test_the_same_document_reads_the_same_on_either_line_ending(tmp_path):
    """Three platforms, one document — the hint must not depend on who wrote it.

    It holds because the reader normalises: extract_text goes through read_text,
    so a carriage return never reaches the strip. This guards that, not the
    strip — a reader that stopped translating would put a stray return at the
    head of every excerpt written on Windows and nowhere else.
    """
    unix = tmp_path / "unix.md"
    unix.write_text(_KNOWLEDGE_COMMIT, encoding="utf-8", newline="")
    windows = tmp_path / "windows.md"
    windows.write_text(_KNOWLEDGE_COMMIT.replace("\n", "\r\n"), encoding="utf-8", newline="")

    metas = []
    for path in (unix, windows):
        backend = _mock_backend()
        index_single_file(path, _mock_embedding_provider(), backend)
        metas.append(backend.vector.add.call_args[0][0][0].meta)

    assert metas[0]["heading"] == metas[1]["heading"]
    assert metas[1]["text"].startswith("# How The Relay Reattributes An Author")
