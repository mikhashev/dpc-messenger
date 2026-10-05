"""The memory index keeps each document's full text, and one row per file.

Two defects, measured on the live stores on 2026-10-05. The keyword channel rebuilt
its whole corpus from `meta["text"]` — a 500-character preview — on every add and
remove, so after the first incremental change only the newest document was searchable
past its opening. And the live write paths (write_file, the L6 commit) appended
without removing the key's old rows or recording the new hash, so the next start
embedded the file again beside them: 140 duplicated keys on agent_001, two ghosts.

The startup sync is the repair for stores written before the fix; the last tests run
it for real against a store degraded to the old shape.
"""
import asyncio
import hashlib
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.bm25_index import TEXTS_FILE, BM25Index
from dpc_client_core.dpc_agent.faiss_index import FaissIndex
from dpc_client_core.dpc_agent.index_writer import writer_for
from dpc_client_core.dpc_agent.indexing_pipeline import (
    doc_hash,
    document_meta,
    index_single_file,
    replace_file_in_index,
)
from dpc_client_core.dpc_agent.retrieval import make_backend_for_agent
from dpc_client_core.dpc_agent.tools.core import repo_delete, write_file
from dpc_client_core.dpc_agent.tools.registry import ToolContext

UNIQUE = "zebraquux"
DIMS = 8


class FakeProvider:
    model_name = "fake-embed"
    dimensions = DIMS
    max_tokens = 4096
    _model = None

    def __init__(self):
        self.embedded = []

    @staticmethod
    def _vec(text):
        return [b / 255 + 0.01 for b in hashlib.sha256(text.encode()).digest()[:DIMS]]

    def embed(self, text):
        self.embedded.append(text)
        return self._vec(text)

    def embed_batch(self, texts):
        self.embedded.extend(texts)
        return [self._vec(t) for t in texts]


def _long_doc(title):
    # The unique word sits past the 500-character preview.
    return f"# {title}\n" + ("filler words about nothing " * 30) + f"\nonly here: {UNIQUE}\n"


def _long_doc_named(name):
    # The same, plus a word only this file has — also past the preview, so finding it
    # proves which document the row was built from.
    return _long_doc(name) + f"and only in this one: {UNIQUE}{name[0]}\n"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / "dpc_home"
    root = home / "agents" / "agent_x"
    (root / "knowledge").mkdir(parents=True)
    (root / "state" / "memory_index").mkdir(parents=True)
    monkeypatch.setenv("DPC_HOME", str(home))
    # The live paths build their backend without dimensions, which asks the real
    # embedding model for them. Nothing here may load a model.
    monkeypatch.setattr("dpc_client_core.dpc_agent.retrieval.factory._derive_embedding_metadata",
                        lambda _cfg: (FakeProvider.model_name, DIMS))
    return root


def _index_dir(root):
    return root / "state" / "memory_index"


def _rows(root):
    """(vector metas, text metas) as stored."""
    backend = make_backend_for_agent(root)
    backend.vector.load()
    backend.text.load()
    return backend.vector._inner._chunks, backend.text._inner._chunk_metas


def _keys(metas):
    return [m["source_file"] for m in metas]


def _keyword_hits(root, query=UNIQUE):
    backend = make_backend_for_agent(root)
    backend.text.load()
    return [m["source_file"] for m, _ in backend.text.search(query, 10)]


def _start(root, provider, monkeypatch):
    """Run the real startup sync of DpcAgentManager and wait for it to land."""
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    monkeypatch.setattr("dpc_client_core.dpc_agent.model_download.is_model_downloaded",
                        lambda _m: True)
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr.agent_id = root.name
    mgr.firewall = None
    mgr.service = None
    mgr.config = {"memory": {"enabled": True, "embedding_model": provider.model_name,
                             "max_tokens": provider.max_tokens}}
    mgr._agent = SimpleNamespace(agent_root=root, _embedding_provider=provider)
    mgr._stop_event = threading.Event()
    mgr._memory_indexes_initialized = False
    asyncio.run(mgr._init_memory_indexes())
    writer_for(_index_dir(root)).submit(lambda: None).result(timeout=120)


def _write(root, name, body):
    (root / "knowledge" / name).write_text(body, encoding="utf-8")


def _ctx(root, provider):
    ctx = ToolContext(agent_root=root)
    ctx._agent = SimpleNamespace(_embedding_provider=provider)
    return ctx


# 1 ---------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["add_another", "remove_another"])
def test_a_word_past_the_preview_survives_an_incremental_change(agent, change):
    provider = FakeProvider()
    for name, body in [("long.md", _long_doc("Long")), ("other.md", "# Other\nshort"),
                       ("third.md", "# Third\nshort too")]:
        _write(agent, name, body)

    backend = make_backend_for_agent(agent, model_name=provider.model_name, dimensions=DIMS)
    for name in ("long.md", "other.md"):
        index_single_file(agent / "knowledge" / name, provider, backend,
                          source_file_key=f"knowledge/{name}")
    backend.save()
    assert _keyword_hits(agent) == ["knowledge/long.md"]

    # A fresh backend, as every live path builds one: load from disk, mutate, save.
    backend = make_backend_for_agent(agent, model_name=provider.model_name, dimensions=DIMS)
    backend.vector.load()
    backend.text.load()
    if change == "add_another":
        index_single_file(agent / "knowledge" / "third.md", provider, backend,
                          source_file_key="knowledge/third.md")
    else:
        backend.vector.remove_by_source("knowledge/other.md")
        backend.text.remove_by_source("knowledge/other.md")
    backend.save()

    assert _keyword_hits(agent) == ["knowledge/long.md"]


# 2 ---------------------------------------------------------------------------

def test_rewriting_a_knowledge_file_leaves_one_current_row_and_its_hash(agent, monkeypatch):
    provider = FakeProvider()
    _write(agent, "seed.md", "# Seed\nthe store needs one document to load")
    _start(agent, provider, monkeypatch)

    ctx = _ctx(agent, provider)
    write_file(ctx, "knowledge/topic.md", "# Topic\nfirst version")
    write_file(ctx, "knowledge/topic.md", "# Topic\nsecond version")

    vec, txt = _rows(agent)
    assert _keys(vec).count("knowledge/topic.md") == 1
    assert _keys(txt).count("knowledge/topic.md") == 1
    for metas in (vec, txt):
        row = next(m for m in metas if m["source_file"] == "knowledge/topic.md")
        assert "second version" in row["text"]
    doc_text, _ = document_meta("knowledge/topic.md", agent / "knowledge" / "topic.md",
                                "# Topic\nsecond version", "L5")
    hashes = json.loads((_index_dir(agent) / "index_meta.json").read_text(encoding="utf-8"))
    assert hashes["file_hashes"]["knowledge/topic.md"] == doc_hash(doc_text)

    # And the next start agrees: nothing to embed, no second row.
    provider.embedded.clear()
    _start(agent, provider, monkeypatch)
    assert provider.embedded == []
    assert _keys(_rows(agent)[0]).count("knowledge/topic.md") == 1


# 3 ---------------------------------------------------------------------------

def test_the_l6_commit_helper_replaces_rather_than_appends(agent, monkeypatch):
    """knowledge_service's L6 reindex is a one-line call to replace_file_in_index;
    its own harness (consensus, firewall, the agent registry) is not built here."""
    provider = FakeProvider()
    _write(agent, "seed.md", "# Seed\nbody")
    _start(agent, provider, monkeypatch)
    commit = agent.parent.parent / "knowledge" / "topic_c1.md"
    commit.parent.mkdir(parents=True)
    commit.write_text("# Commit\nfirst", encoding="utf-8")
    assert replace_file_in_index(agent, commit, provider, "L6", "L6/topic_c1.md")
    commit.write_text("# Commit\nsecond", encoding="utf-8")
    assert replace_file_in_index(agent, commit, provider, "L6", "L6/topic_c1.md")

    vec, txt = _rows(agent)
    assert _keys(vec).count("L6/topic_c1.md") == 1
    assert _keys(txt).count("L6/topic_c1.md") == 1
    assert "second" in next(m for m in vec if m["source_file"] == "L6/topic_c1.md")["text"]


# 4 ---------------------------------------------------------------------------

def _degrade_to_the_old_shape(root, provider):
    """Make the store look like one written before the fix: rows without doc_hash,
    a keyword corpus of previews and no texts file, a ghost, two duplicated keys."""
    d = _index_dir(root)
    vec = FaissIndex(d, model_name=provider.model_name, dimensions=DIMS)
    assert vec.load()
    bm = BM25Index(d)
    assert bm.load()
    for metas in (vec._chunks, bm._chunk_metas):
        for m in metas:
            m.pop("doc_hash", None)

    import numpy as np

    def add(meta):
        vec.add(np.array([provider._vec(meta["text"])], dtype=np.float32), [dict(meta)])
        bm._chunk_metas.append(dict(meta))

    ghost = {"source_file": "knowledge/_tmp_gone.md", "heading": "Gone",
             "source_layer": "L5", "source_path": str(root / "knowledge" / "_tmp_gone.md"),
             "char_count": 10, "text": "# Gone\nbye"}
    add(ghost)
    b_row = next(m for m in vec._chunks if m["source_file"] == "knowledge/b.md")
    # b.md: an old-text row beside the current one — the current one is kept.
    add({**b_row, "text": "# B\nan older draft", "char_count": 17})
    # c.md: two rows, neither current — re-embedded once.
    c_row = next(m for m in vec._chunks if m["source_file"] == "knowledge/c.md")
    stale = {**c_row, "text": "# C\nstale one", "char_count": 13}
    vec.remove_by_source("knowledge/c.md")
    bm._chunk_metas = [m for m in bm._chunk_metas if m["source_file"] != "knowledge/c.md"]
    add(stale)
    add(stale)
    vec.save()
    bm.build([m["text"] for m in bm._chunk_metas], bm._chunk_metas)
    bm.save()
    (d / TEXTS_FILE).unlink()


def test_the_first_start_repairs_an_old_store_and_the_second_changes_nothing(agent, monkeypatch):
    provider = FakeProvider()
    _write(agent, "a.md", _long_doc("A"))
    for name in ("b.md", "c.md", "d.md", "e.md", "f.md"):
        _write(agent, name, f"# {name[0].upper()}\nbody of {name}")
    _start(agent, provider, monkeypatch)
    _degrade_to_the_old_shape(agent, provider)
    assert _keyword_hits(agent) == []          # the old shape: previews only

    provider.embedded.clear()
    _start(agent, provider, monkeypatch)

    assert _keyword_hits(agent) == ["knowledge/a.md"]
    vec, txt = _rows(agent)
    expected = sorted(f"knowledge/{n}" for n in ("a.md", "b.md", "c.md", "d.md", "e.md", "f.md"))
    assert sorted(_keys(vec)) == expected
    assert sorted(_keys(txt)) == expected
    b_vec = next(m for m in vec if m["source_file"] == "knowledge/b.md")
    assert "body of b.md" in b_vec["text"]
    c_text = next(t for t in provider.embedded if "body of c.md" in t)
    assert provider.embedded == [c_text]       # only the key with no current row
    stored = json.loads((_index_dir(agent) / TEXTS_FILE).read_text(encoding="utf-8"))
    assert stored["corpus_version"] == 2 and len(stored["texts"]) == len(txt)
    backups = list((agent.parent.parent / "backups").glob("memory-index-*/agent_x"))
    assert len(backups) == 1 and (backups[0] / "bm25_chunks.json").exists()

    files = sorted(p for p in _index_dir(agent).rglob("*") if p.is_file())
    before = {p: p.read_bytes() for p in files}
    provider.embedded.clear()
    _start(agent, provider, monkeypatch)
    assert provider.embedded == []
    assert {p: p.read_bytes() for p in files} == before


# 5 ---------------------------------------------------------------------------

def test_deleting_a_directory_drops_the_rows_of_the_files_under_it(agent, monkeypatch):
    provider = FakeProvider()
    _write(agent, "keep.md", "# Keep\nstays")
    _start(agent, provider, monkeypatch)
    ctx = _ctx(agent, provider)
    write_file(ctx, "knowledge/sub/x.md", "# X\nunder the directory")
    write_file(ctx, "knowledge/sub/y.md", "# Y\nalso under it")
    assert {"knowledge/sub/x.md", "knowledge/sub/y.md"} <= set(_keys(_rows(agent)[0]))

    assert repo_delete(ctx, "knowledge/sub", recursive=True).startswith("✓")

    vec, txt = _rows(agent)
    assert _keys(vec) == ["knowledge/keep.md"]
    assert _keys(txt) == ["knowledge/keep.md"]


# 6 ---------------------------------------------------------------------------

def _store_of(d, names):
    """A saved keyword store, one long document per name."""
    bodies = {n: _long_doc_named(n) for n in names}
    bm = BM25Index(d)
    bm.build([bodies[n] for n in names],
             [{"source_file": n, "text": bodies[n][:500]} for n in names])
    bm.save()
    return bodies


def test_a_texts_file_from_a_later_generation_is_not_read_as_this_one(agent, caplog):
    """The cut inside `save`: the texts landed, the chunk list did not.

    The mutation is a replace — remove_by_source then add — which moves the key's row
    to the end of both lists, so the two generations have the same row count and only
    their order differs. A length check passes and every row from the moved one on is
    another document's text; the digest is what tells them apart.
    """
    d = _index_dir(agent)
    names = ["a.md", "b.md", "c.md"]
    bodies = _store_of(d, names)
    chunks_before = (d / "bm25_chunks.json").read_bytes()

    bm = BM25Index(d)
    assert bm.load()
    bm.remove_by_source("a.md")
    bm.add([bodies["a.md"]], [{"source_file": "a.md", "text": bodies["a.md"][:500]}])
    bm.save()
    stored = json.loads((d / TEXTS_FILE).read_text(encoding="utf-8"))
    assert stored["texts"][-1].startswith("# a.md")       # the replace moved it to the end
    (d / "bm25_chunks.json").write_bytes(chunks_before)   # the cut

    bm = BM25Index(d)
    assert bm.load()
    assert _keys(bm._chunk_metas) == names                # same count, earlier order
    caplog.set_level(logging.WARNING)
    bm.add(["# D\nbody of d"], [{"source_file": "d.md", "text": "# D\nbody of d"}])
    bm.save()

    stored = json.loads((d / TEXTS_FILE).read_text(encoding="utf-8"))
    assert stored["texts"] == [None, None, None, "# D\nbody of d"]
    # Each file's own word is past its preview, so no row answers for it at all —
    # rather than a row answering under another document's name.
    for name in names:
        assert _keyword_hits(agent, UNIQUE + name[0]) == []
    assert "every row's full text treated as unknown" in caplog.text


def test_a_save_and_load_round_trip_keeps_every_rows_full_text(agent):
    d = _index_dir(agent)
    names = ["a.md", "b.md"]
    _store_of(d, names)

    bm = BM25Index(d)
    assert bm.load()
    bm.add(["# D\nbody of d"], [{"source_file": "d.md", "text": "# D\nbody of d"}])
    bm.save()

    stored = json.loads((d / TEXTS_FILE).read_text(encoding="utf-8"))
    assert None not in stored["texts"] and len(stored["texts"]) == 3
    for name in names:
        assert _keyword_hits(agent, UNIQUE + name[0]) == [name]
