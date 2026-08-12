"""Two writers share `index_meta.json`; neither may erase the other's half.

Each test here fails on the code as it stood on 2026-08-12, and each failure is one of
the two directions that were observed on disk that day: `file_hashes` and
`header.key_format` gone after a knowledge-commit reindex, and a stale `chunks` list
written back over the fresh one on the next per-file sync.
"""

import pathlib

from dpc_client_core.dpc_agent.faiss_index import FaissIndex
from dpc_client_core.dpc_agent.index_meta import read_meta, write_meta


META = "index_meta.json"


def test_a_save_keeps_the_keys_it_does_not_own(tmp_path):
    """The index writes its own half and leaves the manager's alone.

    Red before green: the previous `save()` wrote `{"header", "chunks"}` as a whole
    document, so both assertions below failed — and an absent `key_format` is what made
    the next start re-embed the entire pool.
    """
    meta = tmp_path / META
    write_meta(meta, {
        "header": {"model_name": "BAAI/bge-m3", "key_format": "layer_addressed_v6"},
        "file_hashes": {"L6/a.md": "0123456789abcdef"},
    })

    index = FaissIndex(tmp_path, model_name="BAAI/bge-m3", dimensions=8)
    index.save()

    doc = read_meta(meta)
    assert doc["file_hashes"] == {"L6/a.md": "0123456789abcdef"}
    assert doc["header"]["key_format"] == "layer_addressed_v6"
    assert doc["header"]["model_name"] == "BAAI/bge-m3"
    assert doc["chunks"] == []


def test_a_save_still_owns_its_own_half(tmp_path):
    """Preserving foreign keys must not turn into preserving stale own ones."""
    meta = tmp_path / META
    write_meta(meta, {
        "header": {"model_name": "old-model", "chunk_count": 99, "key_format": "v6"},
        "chunks": [{"source_file": "L6/gone.md"}],
        "file_hashes": {"L6/gone.md": "dead"},
    })

    index = FaissIndex(tmp_path, model_name="BAAI/bge-m3", dimensions=8)
    index.save()

    doc = read_meta(meta)
    assert doc["chunks"] == []
    assert doc["header"]["model_name"] == "BAAI/bge-m3"
    assert doc["header"]["chunk_count"] == 0
    assert doc["header"]["key_format"] == "v6"
    assert doc["file_hashes"] == {"L6/gone.md": "dead"}


def test_the_meta_is_replaced_in_one_step(tmp_path):
    """A reader meets the old document or the new one, never half of either.

    `FaissIndex.load()` catches its own parse failure and returns False, so a torn read
    is not an error anybody sees — it is recall silently returning nothing.
    """
    meta = tmp_path / META
    write_meta(meta, {"header": {}, "file_hashes": {"a": "1"}})
    write_meta(meta, {"header": {}, "file_hashes": {"a": "2"}})

    assert read_meta(meta)["file_hashes"] == {"a": "2"}
    assert not list(tmp_path.glob("*.tmp"))


def test_an_unreadable_meta_reads_as_empty_and_not_as_a_crash(tmp_path):
    meta = tmp_path / META
    meta.write_text("{ not json", encoding="utf-8")
    assert read_meta(meta) == {}
    assert read_meta(tmp_path / "absent.json") == {}


def test_the_per_file_sync_does_not_write_the_meta_document_whole():
    """The other direction, guarded at the source.

    The defect was not a wrong line but a snapshot: `_sync_index` read the meta at the
    start of its run and wrote that dictionary back after `backend.save()` had already
    put a fresh `chunks` list in the file. A unit test cannot reach that closure, and
    the property worth holding is narrow enough to state directly — this path re-reads
    and writes through the shared helper, and never formats the document itself.
    """
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "dpc_client_core" / "managers" / "agent_manager.py").read_text(encoding="utf-8")

    assert "meta_path.write_text(" not in source, (
        "write index_meta.json through write_meta() — a whole-document write here "
        "erases the chunks list the index just saved"
    )
    assert "read_meta(meta_path)" in source
    assert "write_meta(meta_path" in source

    # The rule underneath the two assertions above, and the one that survives a
    # variable being renamed: this writer does not own `chunks` and has no reason to
    # name the key at all. (The word appears here for streaming text, which is why
    # this looks for the JSON key rather than the word.)
    assert '"chunks"' not in source, (
        "agent_manager must not name the index's own key — it owns file_hashes and "
        "header.key_format, nothing else in this document"
    )
