"""The embedding cache hands a vector back only to the model and window that wrote it.

Board entry THE-EMBEDDING-CACHE-DOES-NOT-SAY-WHICH-MODEL-WROTE-IT. The cache used to be
keyed by the text alone and stored float16, so a model or window change got the old
vectors back, a wrong-length file was handed out unchallenged, and a hit differed from a
fresh encode. The index header did not record the window, so nothing forced a rebuild.
"""

import json
import logging
import os
import pathlib
import uuid
from urllib.parse import quote

import numpy as np
import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.dpc_agent.memory import EmbeddingProvider


class _FakeModel:
    """An encoder whose output is float64 and not representable in float32."""

    def __init__(self, dim: int = 4, offset: float = 0.0):
        self.dim = dim
        self.offset = offset
        self.calls = []
        self.max_seq_length = 8192

    def get_embedding_dimension(self):
        return self.dim

    def _one(self, text):
        base = (len(text) % 7 + 1) / 10.0 + self.offset
        return [base + i * 0.0123456789 for i in range(self.dim)]

    def encode(self, texts, normalize_embeddings=True):
        self.calls.append(texts)
        if isinstance(texts, str):
            return np.array(self._one(texts), dtype=np.float64)
        return np.array([self._one(t) for t in texts], dtype=np.float64)


def _provider(model, name="test/model-a", max_tokens=4096):
    p = EmbeddingProvider(model_name=name, max_tokens=max_tokens)
    p._model = model
    return p


def test_a_model_change_misses_the_cache():
    a = _provider(_FakeModel(offset=0.0), name="test/model-a")
    b_model = _FakeModel(offset=0.5)
    b = _provider(b_model, name="test/model-b")

    va = a.embed("same text")
    vb = b.embed("same text")

    assert len(b_model.calls) == 1, "model b was handed model a's cached vector"
    assert va != vb


def test_a_window_change_misses_the_cache():
    model = _FakeModel()
    p = _provider(model, max_tokens=4096)
    p.embed("windowed text")
    p.embed("windowed text")
    assert len(model.calls) == 1  # the second call was a hit

    p.max_tokens = 2048
    p.embed("windowed text")
    assert len(model.calls) == 2, "a window change returned the old window's vector"

    p.embed_batch(["windowed text"])
    assert len(model.calls) == 2  # now cached under 2048 as well


def test_a_wrong_length_file_is_a_miss_and_is_never_returned(caplog):
    model = _FakeModel(dim=4)
    p = _provider(model)
    cache = memory._get_disk_cache()
    path = cache._path("short text", p.model_name, p.max_tokens)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.asarray([0.25, 0.5], dtype=np.float32).tofile(path)

    with caplog.at_level(logging.WARNING, logger=memory.log.name):
        v = p.embed("short text")

    assert len(v) == 4
    assert len(model.calls) == 1
    assert any(str(path) in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    # Rewritten with the right length, so the next call is a hit.
    assert np.fromfile(path, dtype=np.float32).size == 4

    np.asarray([0.25, 0.5], dtype=np.float32).tofile(path)
    vectors = p.embed_batch(["short text"])
    assert len(vectors[0]) == 4 and len(model.calls) == 2


def test_a_cached_and_a_fresh_vector_are_byte_identical():
    model = _FakeModel()
    p = _provider(model)

    fresh = p.embed("the same words")
    cached = p.embed("the same words")
    assert len(model.calls) == 1
    assert fresh == cached
    assert np.array(fresh, dtype=np.float32).tobytes() == np.array(cached, dtype=np.float32).tobytes()

    # The batch path: one text a hit, one a miss, each equal to its single-call twin.
    batch = p.embed_batch(["the same words", "other words!"])
    assert batch[0] == fresh
    assert batch[1] == p.embed("other words!")


def test_the_root_level_files_of_the_old_layout_are_never_read():
    import hashlib

    model = _FakeModel(dim=4)
    p = _provider(model)
    cache = memory._get_disk_cache()
    old = cache._dir / f"{hashlib.sha256('legacy text'.encode()).hexdigest()[:16]}.bin"
    np.asarray([9.0, 9.0, 9.0, 9.0], dtype=np.float32).tofile(old)

    v = p.embed("legacy text")

    assert len(model.calls) == 1
    assert v != [9.0, 9.0, 9.0, 9.0]


def test_a_test_run_never_writes_into_the_real_embedding_cache():
    """conftest points the cache at tmp_path; without that, tests write fake vectors
    into the node's shared cache (three of them were found there, 2026-11-07)."""
    real = pathlib.Path(os.environ.get("DPC_HOME", pathlib.Path.home() / ".dpc")) / "cache" / "embeddings"
    cache = memory._get_disk_cache()
    assert real.resolve() != cache._dir.resolve()
    assert real.resolve() not in cache._dir.resolve().parents

    name = f"test/guard-{uuid.uuid4().hex}"
    _provider(_FakeModel(), name=name).embed("guard text")
    assert not (real / quote(name, safe="")).exists()
    assert (cache._dir / quote(name, safe="")).is_dir()


# --- the index header ---------------------------------------------------------

def _saved_index(tmp_path, max_tokens):
    pytest.importorskip("faiss")
    from dpc_client_core.dpc_agent.faiss_index import FaissIndex

    idx = FaissIndex(tmp_path, model_name="m", dimensions=4)
    idx.add(np.ones((1, 4), dtype=np.float32), [{"source_file": "a.md"}])
    idx._header.max_tokens = max_tokens
    idx.save()
    loaded = FaissIndex(tmp_path, model_name="m", dimensions=4)
    assert loaded.load()
    return loaded


def test_needs_rebuild_on_a_changed_window_or_dimension(tmp_path):
    from dpc_client_core.dpc_agent.retrieval.native import NativeVectorIndex

    idx = _saved_index(tmp_path, max_tokens=4096)
    assert not idx.needs_rebuild("m", max_tokens=4096, dimensions=4)
    assert idx.needs_rebuild("m", max_tokens=2048, dimensions=4)
    assert idx.needs_rebuild("m", max_tokens=4096, dimensions=8)
    assert idx.needs_rebuild("other", max_tokens=4096, dimensions=4)

    native = NativeVectorIndex(tmp_path, model_name="m", dimensions=4)
    assert native.load()
    assert native.needs_rebuild("m", max_tokens=2048, dimensions=4)


def test_a_header_without_window_or_dimension_does_not_ask_for_a_rebuild(tmp_path):
    from dpc_client_core.dpc_agent.faiss_index import FaissIndex

    _saved_index(tmp_path, max_tokens=4096)
    meta_path = tmp_path / "index_meta.json"
    doc = json.loads(meta_path.read_text(encoding="utf-8"))
    doc["header"].pop("max_tokens")
    doc["header"].pop("dimensions")
    meta_path.write_text(json.dumps(doc), encoding="utf-8")

    old = FaissIndex(tmp_path, model_name="m", dimensions=4)
    assert old.load()
    assert not old.needs_rebuild("m", max_tokens=2048, dimensions=8)
    assert old.needs_rebuild("other", max_tokens=2048, dimensions=8)


def test_the_startup_decision_reads_the_window_the_same_way(tmp_path):
    from dpc_client_core.dpc_agent.index_keys import KEY_FORMAT
    from dpc_client_core.dpc_agent.indexing_pipeline import rebuild_decision

    def write(header):
        (tmp_path / "index_meta.json").write_text(json.dumps({"header": header}), encoding="utf-8")

    base = {"model_name": "BAAI/bge-m3", "key_format": KEY_FORMAT, "backend": "native+native"}
    write(base)
    assert not rebuild_decision(tmp_path, "BAAI/bge-m3", "native+native", 2048).needed
    write({**base, "max_tokens": None})
    assert not rebuild_decision(tmp_path, "BAAI/bge-m3", "native+native", 2048).needed
    write({**base, "max_tokens": 4096})
    assert not rebuild_decision(tmp_path, "BAAI/bge-m3", "native+native", 4096).needed
    decision = rebuild_decision(tmp_path, "BAAI/bge-m3", "native+native", 2048)
    assert decision.needed and "4096" in decision.message
