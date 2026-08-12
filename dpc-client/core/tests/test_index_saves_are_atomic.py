"""A reader must meet the old index or the new one, never half of either.

The index writer serialises writers against writers. Readers are serialised against
nobody — recall reads the index on every message and holds nothing while it does —
so a save that writes in place is visible mid-flight. And every reader answers a
truncated file the same way: `load()` catches, returns False, and the turn gets no
hints. Not a crash anybody investigates; silence.
"""

import json
import pathlib
import threading
import time

import numpy as np
import pytest

from dpc_client_core.dpc_agent.bm25_index import BM25Index
from dpc_client_core.dpc_agent.faiss_index import FaissIndex
from dpc_client_core.dpc_agent.index_meta import atomic_write_bytes, atomic_write_text


def test_a_text_file_is_replaced_in_one_step(tmp_path):
    target = tmp_path / "doc.json"
    atomic_write_text(target, '{"v": 1}')
    atomic_write_text(target, '{"v": 2}')

    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    assert not list(tmp_path.glob("*.tmp"))  # nothing left behind either


def test_a_writer_that_makes_its_own_file_still_lands_in_one_step(tmp_path):
    """`faiss.write_index` takes a path, so the temp name is the only way in."""
    target = tmp_path / "vectors.faiss"
    target.write_bytes(b"old")
    seen = []

    def write(path):
        seen.append(pathlib.Path(path).name)
        pathlib.Path(path).write_bytes(b"new")
        # The live file is untouched while the writer is still producing bytes.
        assert target.read_bytes() == b"old"

    atomic_write_bytes(target, write)

    assert seen == ["vectors.faiss.tmp"]
    assert target.read_bytes() == b"new"


def test_the_vector_index_is_never_readable_half_written(tmp_path):
    """The property, end to end: a reader looping over `load()` while a save runs
    either gets the whole index or an explicit no, and never a wrong count."""
    idx = FaissIndex(tmp_path, model_name="m", dimensions=8)
    idx.add(np.ones((40, 8), dtype=np.float32),
            [{"source_file": f"f{i}.md", "text": "x"} for i in range(40)])
    idx.save()

    counts = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            probe = FaissIndex(tmp_path, model_name="m", dimensions=8)
            if probe.load():
                counts.append(probe.total_vectors)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for n in range(41, 60):
        idx.add(np.ones((1, 8), dtype=np.float32), [{"source_file": f"f{n}.md", "text": "x"}])
        idx.save()
        time.sleep(0.001)
    stop.set()
    t.join(timeout=5)

    # Every successful load saw a count the index actually held at some point, and the
    # rows always agreed with the chunk list (`load()` refuses otherwise).
    assert counts, "the reader never managed a load — the test proves nothing"
    assert set(counts) <= set(range(40, 60))


def test_the_bm25_directory_is_swapped_rather_than_written_over(tmp_path):
    """It is a directory, so it cannot land in one step — but it can land whole."""
    index = BM25Index(tmp_path)
    index.build(["kotler indexing", "warren indexing", "forge indexing"],
                [{"source_file": f"f{i}.md", "text": t}
                 for i, t in enumerate(["kotler indexing", "warren indexing", "forge indexing"])])
    index.save()

    first = sorted(p.name for p in (tmp_path / "bm25").iterdir())

    index.add(["muse indexing"], [{"source_file": "f3.md", "text": "muse indexing"}])
    index.save()

    # The live directory is complete, and neither staging name survives the save.
    assert sorted(p.name for p in (tmp_path / "bm25").iterdir()) == first
    assert not (tmp_path / "bm25.new").exists()
    assert not (tmp_path / "bm25.old").exists()

    reopened = BM25Index(tmp_path)
    assert reopened.load()
    assert [m["source_file"] for m, _ in reopened.search("muse", top_k=1)] == ["f3.md"]


def test_a_reader_holding_the_file_delays_the_replace_rather_than_losing_it(tmp_path, monkeypatch):
    """Windows denies a replace while the destination is open, and recall opens the
    index on every message. The save waits the reader out instead of failing."""
    from dpc_client_core.dpc_agent import index_meta

    target = tmp_path / "vectors.faiss"
    target.write_bytes(b"old")
    refusals = {"left": 3}
    real_replace = index_meta.os.replace

    def flaky(src, dst):
        if refusals["left"] > 0:
            refusals["left"] -= 1
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)

    monkeypatch.setattr(index_meta.os, "replace", flaky)
    monkeypatch.setattr(index_meta, "_REPLACE_PAUSE", 0.001)

    index_meta.atomic_write_bytes(target, lambda p: pathlib.Path(p).write_bytes(b"new"))

    assert refusals["left"] == 0  # it kept trying
    assert target.read_bytes() == b"new"


def test_a_reader_that_never_lets_go_loses_the_save_and_keeps_the_index_whole(tmp_path, monkeypatch):
    """The right way round: a lost save shows up in the counts and the next pass
    repairs it, while a torn file shows up as recall silently returning nothing."""
    from dpc_client_core.dpc_agent import index_meta

    target = tmp_path / "vectors.faiss"
    target.write_bytes(b"old")
    monkeypatch.setattr(index_meta, "_REPLACE_PAUSE", 0.001)
    monkeypatch.setattr(index_meta.os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(PermissionError(5, "denied")))

    with pytest.raises(PermissionError):
        index_meta.atomic_write_bytes(target, lambda p: pathlib.Path(p).write_bytes(b"new"))

    assert target.read_bytes() == b"old"


def test_a_save_after_an_interrupted_one_still_lands(tmp_path):
    """A staging directory left by a killed process must not block the next save.

    The writer is a daemon thread now — shutdown can cut it mid-write — so the state
    it leaves behind is a case the next run meets, not a hypothetical.
    """
    (tmp_path / "bm25.new").mkdir()
    (tmp_path / "bm25.new" / "leftover").write_text("junk", encoding="utf-8")

    index = BM25Index(tmp_path)
    index.build(["kotler indexing"], [{"source_file": "f0.md", "text": "kotler indexing"}])
    index.save()

    assert not (tmp_path / "bm25.new").exists()
    reopened = BM25Index(tmp_path)
    assert reopened.load()
    assert reopened.total_documents == 1
