"""ADR-052 L12 point 3, Mike's decision of 2026-11-24 (Zcode's review of follow-up 8, point 2): the small
CPU work that counts as contention for the CPU twin of Whisper is the CPU transcriptions, the embedding
model and GLiNER, while they compute.

The embedding model and GLiNER have no busy signal of their own, so each wraps its compute call in
`cpu_work.computing_on_the_cpu`; the card lease's `_cpu_contention` reads it and L12 names it ("the
embedding model (bge-m3) on the CPU"). A model on a GPU is not CPU work and counts nothing, and a call
that ends, fails or never ran leaves nothing counted. Real `EmbeddingProvider.embed` and
`embed_batch`, and the real `KnowledgeGraph.extract_entities_gliner`, over fake models; the route is
asked through a real Whisper provider with its twin faked.
"""

import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from dpc_client_core import cpu_work
from dpc_client_core.dpc_agent import knowledge_graph, memory
from dpc_client_core.managers import card_lease
from dpc_client_core.providers.whisper_provider import transcribe_with_route

from tests._card_lease_helpers import BOUND, fast, nothing_left  # noqa: F401
from tests._whisper_lease_helpers import cpu_twin, gpu_whisper, host_ram, l12_settings
from tests.test_music_swaps_out_an_idle_supervisor_when_nothing_waits import quick, running_strata  # noqa: F401
from tests.test_one_local_engine_holds_the_card import card  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast", "quick", "card")

EMBEDDER = "the embedding model (bge-m3) on the CPU"


class FakeEmbeddingModel:
    """Records what the CPU work reads while `encode` runs, and can hold the call open."""

    def __init__(self):
        self.during = []
        self.inside = threading.Event()
        self.go = threading.Event()
        self.go.set()

    def encode(self, texts, normalize_embeddings=True):
        import numpy as np

        self.during.append(cpu_work.computing_now())
        self.inside.set()
        assert self.go.wait(5)
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


def embedder(monkeypatch, device):
    monkeypatch.setattr(memory, "_get_disk_cache", lambda: NoCache())
    provider = memory.EmbeddingProvider(model_name="bge-m3", device=device)
    provider._model = FakeEmbeddingModel()
    return provider


def test_nothing_computes_on_the_cpu_when_nothing_runs():
    assert cpu_work.computing_now() == [] and card_lease._cpu_contention() == []


@pytest.mark.parametrize("call", ["embed", "embed_batch"])
def test_the_embedder_counts_while_it_encodes_on_the_cpu_and_not_after(monkeypatch, call):
    provider = embedder(monkeypatch, "cpu")
    if call == "embed":
        provider.embed("one text")
    else:
        provider.embed_batch(["one text", "another"])
    assert provider._model.during == [[EMBEDDER]]
    assert cpu_work.computing_now() == []


def test_an_embedder_on_a_gpu_counts_nothing(monkeypatch):
    """On the card means inside a stint that holds it (ADR-052 3.4): the model is there, not on the CPU."""
    provider = embedder(monkeypatch, "cuda")
    provider._card_client()._on_gpu = True
    provider.embed("one text")
    provider.embed_batch(["a", "b"])
    assert provider._model.during == [[], []]


def test_a_cuda_embedder_outside_a_stint_computes_on_the_cpu_and_counts(monkeypatch):
    """`cuda` is lease-gated since the card lease (ADR-052 3.4): with no stint holding the card the
    model is on the CPU, and that is CPU work."""
    provider = embedder(monkeypatch, "cuda")
    provider.embed("one text")
    provider.embed_batch(["a", "b"])
    assert provider._model.during == [[EMBEDDER], [EMBEDDER]]


@pytest.mark.parametrize("cuda, mps, counted", [
    (False, False, True),   # `auto` resolves to the CPU: counted, as Whisper and GLiNER count what they run on
    (True, False, True),    # `auto` resolves to CUDA, lease-gated (ADR-052 3.4): on the CPU outside a stint
    (False, True, False),   # `auto` resolves to MPS (Apple silicon): not CPU work
])
def test_an_embedder_on_device_auto_counts_the_device_it_resolved_to(monkeypatch, cuda, mps, counted):
    """Follow-up 10 (Zcode #14): `[memory] embedding_device = auto` (and a profile's `auto`) reach the
    provider as no device at all, and the provider resolves it (cuda, then mps, then cpu); what counts is
    that resolved device, never the word the settings carried."""
    fake_torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(memory, "_singleton_providers", {})
    monkeypatch.setattr(memory, "_node_embedding_device", None)
    monkeypatch.setattr(memory, "_get_disk_cache", lambda: NoCache())
    provider = memory.get_embedding_provider("bge-m3", device="auto")
    provider._model = FakeEmbeddingModel()

    provider.embed("one text")
    provider.embed_batch(["a", "b"])

    assert provider._model.during == ([[EMBEDDER], [EMBEDDER]] if counted else [[], []])
    assert cpu_work.computing_now() == []


def test_a_failing_encode_leaves_nothing_counted(monkeypatch):
    provider = embedder(monkeypatch, "cpu")

    def boom(texts, normalize_embeddings=True):
        raise RuntimeError("encode failed")

    provider._model.encode = boom
    with pytest.raises(RuntimeError):
        provider.embed("one text")
    assert cpu_work.computing_now() == []


def test_gliner_counts_while_it_predicts_on_the_cpu_and_not_on_a_gpu(monkeypatch):
    seen = []

    class Model:
        def predict_entities(self, text, labels, threshold=0.5):
            seen.append(cpu_work.computing_now())
            return []

    monkeypatch.setattr(knowledge_graph, "_get_gliner_model", lambda device=None: Model())
    graph = SimpleNamespace()
    texts = [{"source_id": "a", "text": "x" * 40}]
    monkeypatch.setattr(knowledge_graph, "_GLINER_DEVICE", "cpu")
    knowledge_graph.KnowledgeGraph.extract_entities_gliner(graph, texts)
    monkeypatch.setattr(knowledge_graph, "_GLINER_DEVICE", "cuda")
    knowledge_graph.KnowledgeGraph.extract_entities_gliner(graph, texts)
    assert seen == [["GLiNER on the CPU"], []]
    assert cpu_work.computing_now() == []


@pytest.mark.asyncio
async def test_an_embedding_that_computes_is_named_and_multiplies_the_twins_estimate(tmp_path, monkeypatch):
    await running_strata(tmp_path)  # idle in the card's way: the choice is L12's, and Strata is no contention
    host_ram(monkeypatch, 64_000)
    l12_settings(monkeypatch, cpu_contention_factor=2.0)
    provider = embedder(monkeypatch, "cpu")
    provider._model.go.clear()
    worker = asyncio.create_task(asyncio.to_thread(provider.embed, "one text"))
    await asyncio.wait_for(asyncio.to_thread(provider._model.inside.wait, 5), BOUND)
    p = gpu_whisper(monkeypatch)
    twin = cpu_twin(monkeypatch, p)
    plain = twin.expected_seconds(60.0)
    try:
        result = await asyncio.wait_for(transcribe_with_route(
            p, "f", purpose="dictation", wait_s=BOUND, expected_s=p.expected_seconds(60.0), audio_s=60.0), BOUND)
    finally:
        provider._model.go.set()
        await asyncio.wait_for(worker, BOUND)
    assert result["device"] == "cpu"
    assert p.last_route["contention"] == [EMBEDDER] and p.last_route["factor"] == 2.0
    assert p.last_route["cpu_s"] == pytest.approx(2.0 * plain)
    assert f"x2 for {EMBEDDER}" in p.last_route["why"]
    await nothing_left()
