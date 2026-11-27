"""The embedding model (bge-m3) reaches the card only through the card lease (ADR-052 3.4; backlog
THE-EMBEDDER-GLINER-AND-SKILL-EMBEDDINGS-TAKE-THE-GPU-OUTSIDE-THE-CARD-LEASE-...).

`cpu` (and `mps`) never ask. `cuda` and auto on a CUDA machine are lease-gated: the model loads on the
CPU, moves to the card only inside `card_stint` (one lease use for a bulk batch), moves back when the
batch ends or an owner's request waits, and a single `embed` never waits for the card. A move and an
encode never overlap. Real `EmbeddingProvider` and `CardLease`, a fake model, no GPU.
"""

import asyncio
import threading

import pytest

from dpc_client_core.dpc_agent import memory
from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests._card_lease_helpers import BOUND, fast, hold_open, holding, nothing_left, until, waiting, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")

HOLDER = "embedder:BAAI/bge-m3"


class FakeSentenceTransformer:
    """Where the weights are, every move (device, dtype), and where each encode ran."""

    def __init__(self):
        self.where = "cpu"
        self.moves = []
        self.encoded_on = []
        self.gate = None
        self.moving = threading.Event()

    def to(self, device=None, dtype=None):
        self.moving.set()
        if self.gate is not None and device == "cuda":
            assert self.gate.wait(BOUND)
        self.where = device
        self.moves.append((device, str(dtype).replace("torch.", "")))
        return self

    def encode(self, texts, normalize_embeddings=True):
        import numpy as np

        self.encoded_on.append(self.where)
        if isinstance(texts, str):
            return np.zeros(4, dtype=np.float32)
        return np.zeros((len(texts), 4), dtype=np.float32)

    def get_sentence_embedding_dimension(self):
        return 4


class NoCache:
    def get(self, *a, **kw):
        return None

    def put(self, *a, **kw):
        pass


@pytest.fixture(autouse=True)
def _no_disk_cache(monkeypatch):
    monkeypatch.setattr(memory, "_get_disk_cache", lambda: NoCache())


def provider(device):
    p = memory.EmbeddingProvider(model_name="BAAI/bge-m3", device=device)
    p._model = FakeSentenceTransformer()
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("device", ["cpu", "mps"])
async def test_a_cpu_or_mps_setting_never_asks_for_the_card(device):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    p = provider(device)

    def bulk():
        with p.card_stint("index rebuild", 500) as where:
            p.embed_batch([f"doc {i}" for i in range(8)])
            return where

    assert await asyncio.wait_for(asyncio.to_thread(bulk), BOUND) == device
    assert not p.lease_gated and p._card is None
    assert card_lease.lease_named(HOLDER) is None and holding() == [] and waiting() == []
    assert p._model.moves == []


def test_a_lease_gated_model_outside_a_stint_computes_on_the_cpu_with_no_owner_loop():
    p = provider("cuda")
    p.embed("a query")
    p.embed_batch(["a", "b"])
    assert p.lease_gated and p.compute_device == "cpu"
    assert p._model.encoded_on == ["cpu", "cpu"] and p._model.moves == []
    with p.card_stint("index rebuild", 500) as where:
        p.embed_batch(["c"])
    assert where == "cpu" and p._model.moves == [], "no owner loop, no lease: the CPU"


@pytest.mark.asyncio
async def test_a_query_never_waits_for_the_card_another_holder_has():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    p = provider("cuda")
    w = whisper()
    release = asyncio.Event()
    holder = asyncio.create_task(hold_open(w.lease, "dictation", release))
    await until(lambda: holding() == ["whisper:turbo"])

    await asyncio.wait_for(asyncio.to_thread(p.embed, "a query"), 1.0)
    assert p._model.encoded_on == ["cpu"] and waiting() == [] and p._model.moves == []
    release.set()
    await asyncio.wait_for(holder, BOUND)


@pytest.mark.asyncio
async def test_a_bulk_pass_takes_one_use_moves_to_the_card_in_fp16_and_back_and_lets_go():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    p = provider("cuda")
    seen = {}

    def bulk():
        with p.card_stint("index rebuild", 40) as where:
            seen["where"] = where
            seen["uses"] = [u.what for u in card_lease.lease_named(HOLDER)._uses]
            seen["row"] = next(r for r in base_mod.card_holders() if r["alias"] == HOLDER)
            for i in range(0, 8, 4):
                p.embed_batch([f"doc {j}" for j in range(i, i + 4)])
            p.embed("a query that rides the pass")

    await asyncio.wait_for(asyncio.to_thread(bulk), BOUND)
    lease = card_lease.lease_named(HOLDER)
    await until(lambda: not lease.granted)
    assert seen["where"] == "cuda" and seen["uses"] == ["index rebuild"]
    assert seen["row"]["display"] == "Embedding model (bge-m3)" and seen["row"]["kind"] == "embedding"
    assert p._model.encoded_on == ["cuda", "cuda", "cuda"]
    assert p._model.moves[0] == ("cuda", "float16")
    assert p._model.moves[-1][0] == "cpu" and len(p._model.moves) == 2
    assert p.compute_device == "cpu"
    await nothing_left()


@pytest.mark.asyncio
async def test_an_owner_request_mid_pass_sends_the_model_back_and_the_pass_goes_on_on_the_cpu():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    p = provider("cuda")
    first = threading.Event()
    go_on = threading.Event()

    def bulk():
        with p.card_stint("index rebuild", 40):
            p.embed_batch(["a", "b", "c", "d"])
            first.set()
            assert go_on.wait(BOUND)
            p.embed_batch(["e", "f", "g", "h"])
            p.embed_batch(["i", "j"])

    thread = asyncio.create_task(asyncio.to_thread(bulk))
    await asyncio.wait_for(asyncio.to_thread(first.wait, BOUND), BOUND)
    w = whisper()

    async def dictation():
        async with w.lease.use("dictation", wait_s=BOUND):
            return holding()

    dictating = asyncio.create_task(dictation())
    await until(lambda: waiting() == ["whisper:turbo"])
    go_on.set()
    assert await asyncio.wait_for(dictating, BOUND) == ["whisper:turbo"]
    await asyncio.wait_for(thread, BOUND)
    assert p._model.encoded_on == ["cuda", "cpu", "cpu"]
    assert [m[0] for m in p._model.moves] == ["cuda", "cpu"]
    await nothing_left()


@pytest.mark.asyncio
async def test_an_encode_during_the_move_to_the_card_waits_for_it():
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    p = provider("cuda")
    p._model.gate = threading.Event()
    done = threading.Event()

    def bulk():
        with p.card_stint("index rebuild", 40):
            assert done.wait(BOUND)

    thread = asyncio.create_task(asyncio.to_thread(bulk))
    await asyncio.wait_for(asyncio.to_thread(p._model.moving.wait, BOUND), BOUND)
    query = asyncio.create_task(asyncio.to_thread(p.embed, "a query"))
    await asyncio.sleep(0.1)
    assert p._model.encoded_on == [] and not query.done(), "no encode mid-move"
    p._model.gate.set()
    await asyncio.wait_for(query, BOUND)
    assert p._model.encoded_on == ["cuda"]
    done.set()
    await asyncio.wait_for(thread, BOUND)


def test_unload_drops_the_model_under_the_device_lock_and_frees_device_memory(monkeypatch):
    freed = []
    monkeypatch.setattr(memory, "_release_device_memory", lambda: freed.append(True))
    p = provider("cuda")
    card = p._card_client()
    held = threading.Event()
    release = threading.Event()

    def holder():
        with card.device_lock:
            held.set()
            release.wait(BOUND)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(BOUND)
    unloaded = threading.Thread(target=p.unload)
    unloaded.start()
    unloaded.join(0.1)
    assert p._model is not None, "waits for the device lock"
    release.set()
    unloaded.join(BOUND)
    t.join(BOUND)
    assert p._model is None and freed == [True]
