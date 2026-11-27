"""The start-up index pass takes one card lease use for the whole pass (ADR-052 3.4; backlog
THE-EMBEDDER-GLINER-AND-SKILL-EMBEDDINGS-TAKE-THE-GPU-OUTSIDE-THE-CARD-LEASE-..., "especially during
bulk indexing at startup").

The real `DpcAgentManager._init_memory_indexes` on the index writer and the real `full_rebuild`, with a
real lease-gated `EmbeddingProvider` over a fake model and an owner loop in its own thread: every
batch is embedded on the card inside the one use the pass opened, the model moves to the card once and
back once, and the card is let go when the pass ends. A provider that is not an `EmbeddingProvider`
(a fake, a remote one) gets no stint. No GPU.
"""

import asyncio
import hashlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.dpc_agent.index_writer import writer_for
from dpc_client_core.dpc_agent.indexing_pipeline import bulk_embedding, full_rebuild
from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod

DIMS = 8
MODEL = "fake-embed"
HOLDER = f"embedder:{MODEL}"


class FakeSentenceTransformer:
    """Records where each batch was encoded and which lease uses were open meanwhile."""

    max_seq_length = 8192

    def __init__(self):
        self.where = "cpu"
        self.moves = []
        self.batches = []

    def to(self, device=None, dtype=None):
        self.where = device
        self.moves.append(device)
        return self

    def encode(self, texts, normalize_embeddings=True):
        lease = card_lease.lease_named(HOLDER)
        self.batches.append((self.where, [u.what for u in lease._uses] if lease is not None else []))
        single = isinstance(texts, str)
        rows = [[b / 255 + 0.01 for b in hashlib.sha256(t.encode()).digest()[:DIMS]]
                for t in ([texts] if single else texts)]
        arr = np.asarray(rows, dtype=np.float32)
        arr /= np.linalg.norm(arr, axis=1, keepdims=True)
        return arr[0] if single else arr

    def get_sentence_embedding_dimension(self):
        return DIMS


class NoCache:
    def get(self, *a, **kw):
        return None

    def put(self, *a, **kw):
        pass


@pytest.fixture
def owner_loop():
    """The service's loop, in its own thread, as at run time."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    base_mod.bind_owner_loop(loop)
    yield loop
    base_mod.bind_owner_loop(None)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    loop.close()


@pytest.fixture
def gated_provider(monkeypatch):
    monkeypatch.setattr(memory, "_get_disk_cache", lambda: NoCache())
    monkeypatch.setattr(memory, "_release_device_memory", lambda: None)
    provider = memory.EmbeddingProvider(model_name=MODEL, device="cuda")
    provider._model = FakeSentenceTransformer()
    return provider


def _wait_released(timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        lease = card_lease.lease_named(HOLDER)
        if lease is not None and not lease.granted and not lease._uses:
            return lease
        time.sleep(0.01)
    raise AssertionError("the card was not let go")


def test_the_startup_pass_embeds_every_batch_on_the_card_inside_one_use(tmp_path, monkeypatch, owner_loop,
                                                                       gated_provider):
    from dpc_client_core.managers.agent_manager import DpcAgentManager

    home = tmp_path / "dpc_home"
    root = home / "agents" / "agent_x"
    (root / "knowledge").mkdir(parents=True)
    (root / "state" / "memory_index").mkdir(parents=True)
    monkeypatch.setenv("DPC_HOME", str(home))
    monkeypatch.setattr("dpc_client_core.dpc_agent.model_download.is_model_downloaded", lambda _m: True)
    for i in range(24):
        (root / "knowledge" / f"note_{i:02d}.md").write_text(f"# Note {i}\nbody of note number {i}\n",
                                                             encoding="utf-8")

    mgr = DpcAgentManager.__new__(DpcAgentManager)
    mgr.agent_id = root.name
    mgr.firewall = None
    mgr.service = None
    mgr.config = {"memory": {"enabled": True, "embedding_model": MODEL, "max_tokens": 4096, "batch_size": 4}}
    mgr._agent = SimpleNamespace(agent_root=root, _embedding_provider=gated_provider)
    mgr._stop_event = threading.Event()
    mgr._memory_indexes_initialized = False
    asyncio.run(mgr._init_memory_indexes())
    writer_for(root / "state" / "memory_index").submit(lambda: None).result(timeout=120)

    model = gated_provider._model
    assert len(model.batches) == 6, model.batches
    assert model.batches == [("cuda", ["index rebuild"])] * 6
    assert model.moves == ["cuda", "cpu"]
    lease = _wait_released()
    assert lease.display == "Embedding model (fake-embed)"
    assert card_lease._LEASE_LINE == []


def test_full_rebuild_holds_one_use_for_all_its_batches(tmp_path, owner_loop, gated_provider):
    for i in range(16):
        (tmp_path / f"doc_{i:02d}.md").write_text(f"Document {i} content", encoding="utf-8")
    backend = MagicMock()

    def rebuild():
        return full_rebuild(tmp_path, gated_provider, backend)

    count = asyncio.run(asyncio.wait_for(asyncio.to_thread(rebuild), 30))
    assert count == 16
    model = gated_provider._model
    assert model.batches == [("cuda", ["index rebuild"])] * 4
    assert model.moves == ["cuda", "cpu"]
    _wait_released()


def test_a_provider_that_is_not_an_embedding_provider_gets_no_stint():
    fake = MagicMock()
    with bulk_embedding(fake, 1000):
        pass
    fake.card_stint.assert_not_called()
