"""The per-file index pass says at its start how many documents it hands to the embedder, and
its summary counts the pass's real cache hits and misses — an all-miss pass included.

Board entry SMALL-INDEX-UPDATES-RUN-GLINER-AND-THE-EMBEDDER-ON-CPU-AND-TAKE-MINUTES-SINCE-
THE-CARD-LEASE-MERGE, appended decision (Mike approved 2026-11-27). The only cache line
`embed_batch` printed was a DEBUG one guarded by `hits > 0`, so the pass the card is about —
a small update over a cold or lost cache, every text a miss — ended with no cache numbers in
the log at all, beside a summary that could read like minutes spent for nothing. The counts
now come from a per-pass accumulator handed to each `embed_batch` call, never from that line
or from provider state: the provider is one per process and two agents' writer threads run
their passes on it at the same time.

The real `DpcAgentManager._init_memory_indexes` startup sync on the index writer, with a real
`EmbeddingProvider` over a fake model and the test's own disk cache (conftest); the counters
seam of `embed_batch` is pinned directly beside it. No GPU.
"""

import asyncio
import hashlib
import logging
import re
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.dpc_agent.index_writer import writer_for
from dpc_client_core.dpc_agent.memory import EmbeddingCacheCounters, EmbeddingProvider

DIMS = 8
MODEL = "fake-embed"
PASS_LOG = "dpc_client_core.managers.agent_manager"
_END_NUMBERS = re.compile(r"cache (\d+) hits / (\d+) misses")


class _FakeModel:
    """A deterministic encoder that records every text it was really asked to encode."""

    max_seq_length = 8192

    def __init__(self, dim: int = DIMS):
        self._dim = dim
        self.calls: list = []

    def get_embedding_dimension(self):
        return self._dim

    def encode(self, texts, normalize_embeddings=True):
        self.calls.append([texts] if isinstance(texts, str) else list(texts))
        rows = [[b / 255 + 0.01 for b in hashlib.sha256(t.encode()).digest()[:self._dim]]
                for t in ([texts] if isinstance(texts, str) else texts)]
        arr = np.asarray(rows, dtype=np.float32)
        arr /= np.linalg.norm(arr, axis=1, keepdims=True)
        return arr[0] if isinstance(texts, str) else arr


@pytest.fixture
def agent(tmp_path, monkeypatch):
    home = tmp_path / "dpc_home"
    root = home / "agents" / "agent_x"
    (root / "knowledge").mkdir(parents=True)
    (root / "state" / "memory_index").mkdir(parents=True)
    monkeypatch.setenv("DPC_HOME", str(home))
    return root


@pytest.fixture
def pass_log(caplog):
    caplog.set_level(logging.INFO, logger=PASS_LOG)
    return caplog


def _provider():
    """A real provider over the fake model, on the CPU, never touching the process singleton."""
    provider = EmbeddingProvider(model_name=MODEL, device="cpu", max_tokens=4096)
    provider._model = _FakeModel()
    return provider


def _index_dir(root):
    return root / "state" / "memory_index"


def _write(root, name, body):
    (root / "knowledge" / name).write_text(body, encoding="utf-8")


def _start(root, provider, monkeypatch):
    """Run the real startup sync of DpcAgentManager and wait for the pass to land."""
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    monkeypatch.setattr("dpc_client_core.dpc_agent.model_download.is_model_downloaded",
                        lambda _m: True)
    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr.agent_id = root.name
    mgr.firewall = None
    mgr.service = None
    # batch_size 2 over 3 documents: two embed_batch calls, so the counters are seen
    # accumulating across a pass's calls, not reset per batch.
    mgr.config = {"memory": {"enabled": True, "embedding_model": provider.model_name,
                             "max_tokens": provider.max_tokens, "batch_size": 2}}
    mgr._agent = SimpleNamespace(agent_root=root, _embedding_provider=provider)
    mgr._stop_event = threading.Event()
    mgr._memory_indexes_initialized = False
    asyncio.run(mgr._init_memory_indexes())
    writer_for(_index_dir(root)).submit(lambda: None).result(timeout=120)


def _summaries(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == PASS_LOG and r.getMessage().startswith("Per-file index update: ")]


def _start_lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == PASS_LOG and "documents to embed this pass" in r.getMessage()]


def _assert_the_pass_did_not_fail(caplog):
    failed = [r.getMessage() for r in caplog.records
              if "Background memory indexing failed" in r.getMessage()
              or "memory index pass failed" in r.getMessage()]
    assert not failed, failed


def _encoded_texts(provider):
    return [t for call in provider._model.calls for t in call]


# 1 ---------------------------------------------------------------------------

def test_an_all_miss_pass_logs_zero_hits_and_the_true_miss_count(agent, monkeypatch, pass_log):
    """The regression this file exists for: a cold cache means no DEBUG line ever prints, and
    the summary must still say hits=0 with the real miss count."""
    provider = _provider()
    for i in range(3):
        _write(agent, f"note_{i}.md", f"# Note {i}\nbody of note number {i}\n")

    _start(agent, provider, monkeypatch)

    _assert_the_pass_did_not_fail(pass_log)
    assert _start_lines(pass_log) == [
        f"[{agent.name}] Per-file index update: 3 documents to embed this pass"]
    (summary,) = _summaries(pass_log)
    numbers = _END_NUMBERS.search(summary)
    assert numbers, summary
    assert (int(numbers.group(1)), int(numbers.group(2))) == (0, 3)
    # The misses are the real ones — every one of them was encoded — and they were
    # accumulated across the pass's two embed_batch calls (batch_size 2 over 3 docs).
    assert len(provider._model.calls) == 2
    assert len(_encoded_texts(provider)) == 3
    assert ", on cpu, cache 0 hits / 3 misses)" in summary


# 2 ---------------------------------------------------------------------------

def test_a_small_update_of_one_changed_file_still_logs_zero_hits_and_its_one_miss(
        agent, monkeypatch, pass_log):
    """The card's own shape: a warm index, one file changed, one text to embed and it is a
    miss — the exact pass the old hits>0 DEBUG line said nothing about."""
    provider = _provider()
    for i in range(3):
        _write(agent, f"note_{i}.md", f"# Note {i}\nbody of note number {i}\n")
    _start(agent, provider, monkeypatch)          # warms the cache: 3 misses, logged as (0, 3)
    assert _END_NUMBERS.search(_summaries(pass_log)[0])

    pass_log.clear()
    provider._model.calls.clear()
    _write(agent, "note_1.md", "# Note 1\na changed body\n")
    _start(agent, provider, monkeypatch)

    _assert_the_pass_did_not_fail(pass_log)
    assert _start_lines(pass_log) == [
        f"[{agent.name}] Per-file index update: 1 documents to embed this pass"]
    (summary,) = _summaries(pass_log)
    numbers = _END_NUMBERS.search(summary)
    assert (int(numbers.group(1)), int(numbers.group(2))) == (0, 1)
    assert len(_encoded_texts(provider)) == 1
    assert "a changed body" in _encoded_texts(provider)[0]


# 3 ---------------------------------------------------------------------------

def test_a_pass_over_a_lost_file_map_logs_its_mixed_hits_and_misses(agent, monkeypatch, pass_log):
    """The mixed case: the file map is gone (the crash shape `index_meta.json` is the victim
    of), so every document is handed to the embedder again — the unchanged texts come from
    the cache, the changed one is encoded."""
    provider = _provider()
    for i in range(3):
        _write(agent, f"note_{i}.md", f"# Note {i}\nbody of note number {i}\n")
    _start(agent, provider, monkeypatch)          # warms the cache
    pass_log.clear()
    provider._model.calls.clear()

    _write(agent, "note_2.md", "# Note 2\na changed body\n")
    (_index_dir(agent) / "index_meta.json").unlink()
    _start(agent, provider, monkeypatch)

    _assert_the_pass_did_not_fail(pass_log)
    assert _start_lines(pass_log) == [
        f"[{agent.name}] Per-file index update: 3 documents to embed this pass"]
    (summary,) = _summaries(pass_log)
    numbers = _END_NUMBERS.search(summary)
    assert (int(numbers.group(1)), int(numbers.group(2))) == (2, 1)
    # Only the changed text reached the encoder; the two hits never did.
    assert len(_encoded_texts(provider)) == 1
    assert "a changed body" in _encoded_texts(provider)[0]


# 4 -- the counters seam of embed_batch, without the pass around it ------------

def test_embed_batch_counts_into_the_callers_object_and_the_debug_line_keeps_its_guard(caplog):
    provider = _provider()
    with caplog.at_level(logging.DEBUG, logger=memory.log.name):
        all_miss = EmbeddingCacheCounters()
        provider.embed_batch(["a cold text", "another cold one"], counters=all_miss)
        assert (all_miss.hits, all_miss.misses) == (0, 2)
        # The guard is untouched: an all-miss batch still prints nothing.
        assert not [r for r in caplog.records if "Embedding cache:" in r.getMessage()]

        mixed = EmbeddingCacheCounters()
        provider.embed_batch(["a cold text", "a third text"], counters=mixed)
        assert (mixed.hits, mixed.misses) == (1, 1)
        (debug,) = [r.getMessage() for r in caplog.records
                    if "Embedding cache:" in r.getMessage()]
        assert debug == "Embedding cache: 1 hits, 1 misses out of 2 texts"
        # The provider singleton carries no state: the second call's counts stayed
        # out of the first caller's object.
        assert all_miss == EmbeddingCacheCounters(hits=0, misses=2)

    # The call without a counter behaves as before.
    vectors = provider.embed_batch(["a cold text"])
    assert len(vectors[0]) == DIMS


def test_two_passes_sharing_the_provider_keep_their_counts_apart():
    """The design constraint as such: one provider, two accumulator objects, interleaved
    calls — each object holds only its own pass's outcomes."""
    provider = _provider()
    first, second = EmbeddingCacheCounters(), EmbeddingCacheCounters()

    provider.embed_batch(["shared provider, first pass"], counters=first)
    provider.embed_batch(["shared provider, second pass one",
                          "shared provider, second pass two"], counters=second)
    provider.embed_batch(["shared provider, first pass"], counters=first)   # now a hit

    assert first == EmbeddingCacheCounters(hits=1, misses=1)
    assert second == EmbeddingCacheCounters(hits=0, misses=2)
