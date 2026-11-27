"""GLiNER reaches the card only through the card lease (ADR-052 3.4, S7; backlog
THE-EMBEDDER-GLINER-AND-SKILL-EMBEDDINGS-TAKE-THE-GPU-OUTSIDE-THE-CARD-LEASE-...).

`cpu` never asks. `cuda` and auto on a CUDA machine load on the CPU; a sleep pass's entity extraction
over many texts takes one lease use ("entity extraction"), moves the model to the card, lets go before
its next text when an owner's request waits, and moves back when it ends. A single text never asks.
The real `_get_gliner_model` and `extract_entities_gliner` over a fake GLiNER; no GPU.
"""

import asyncio
import sys
import threading
import types
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import knowledge_graph as kg
from dpc_client_core.managers import card_lease
from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests._card_lease_helpers import BOUND, fast, holding, nothing_left, until, waiting, whisper  # noqa: F401

pytestmark = pytest.mark.usefixtures("fast")

HOLDER = "ner:gliner"


class FakeGLiNER:
    def __init__(self):
        self.where = None
        self.moves = []
        self.predicted_on = []
        self.after_first = None

    def to(self, device):
        self.where = device
        self.moves.append(device)
        return self

    def predict_entities(self, text, labels, threshold=0.5):
        self.predicted_on.append(self.where)
        if self.after_first is not None and len(self.predicted_on) == 1:
            self.after_first()
        return [{"text": "Ada Lovelace", "label": "person", "score": 0.9}]


@pytest.fixture
def gliner(monkeypatch):
    """A fake gliner module and a machine that says it has CUDA, with the singleton reset."""
    made = {}

    class GLiNER:
        @staticmethod
        def from_pretrained(name):
            made["model"] = FakeGLiNER()
            return made["model"]

    module = types.ModuleType("gliner")
    module.GLiNER = GLiNER
    monkeypatch.setitem(sys.modules, "gliner", module)
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    from dpc_client_core.dpc_agent import memory
    monkeypatch.setattr(memory, "_release_device_memory", lambda: None)
    for name, value in (("_GLINER_MODEL", None), ("_GLINER_DEVICE", None), ("_GLINER_GATED", False),
                        ("_GLINER_CARD", None)):
        monkeypatch.setattr(kg, name, value)
    return made


def texts(n):
    return [{"source_id": f"doc-{i}", "text": f"Ada Lovelace wrote the first program, note {i}."} for i in range(n)]


def extract(device, items):
    graph = SimpleNamespace(_gliner_device=device)
    return kg.KnowledgeGraph.extract_entities_gliner(graph, items)


@pytest.mark.asyncio
async def test_cpu_never_asks_for_the_card(gliner):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    found = await asyncio.wait_for(asyncio.to_thread(extract, "cpu", texts(30)), BOUND)
    assert len(found) == 30
    assert gliner["model"].moves == ["cpu"] and set(gliner["model"].predicted_on) == {"cpu"}
    assert card_lease.lease_named(HOLDER) is None and holding() == []


@pytest.mark.asyncio
async def test_a_pass_over_many_texts_holds_one_use_on_the_card_and_moves_back(gliner):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    seen = {}

    def note():
        lease = card_lease.lease_named(HOLDER)
        seen["uses"] = [u.what for u in lease._uses]
        seen["row"] = next(r for r in base_mod.card_holders() if r["alias"] == HOLDER)

    await asyncio.wait_for(asyncio.to_thread(kg._get_gliner_model, "auto"), BOUND)
    gliner["model"].after_first = note
    found = await asyncio.wait_for(asyncio.to_thread(extract, "auto", texts(30)), BOUND)
    lease = card_lease.lease_named(HOLDER)
    await until(lambda: not lease.granted)
    assert len(found) == 30
    assert seen["uses"] == ["entity extraction"]
    assert seen["row"]["display"] == "GLiNER (gliner_multi-v2.1)" and seen["row"]["kind"] == "ner"
    assert gliner["model"].moves == ["cpu", "cuda", "cpu"]
    assert set(gliner["model"].predicted_on) == {"cuda"}
    assert kg._GLINER_DEVICE == "cpu"
    await nothing_left()


@pytest.mark.asyncio
async def test_a_single_text_never_asks_and_runs_on_the_cpu(gliner):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    found = await asyncio.wait_for(asyncio.to_thread(extract, "cuda", texts(1)), BOUND)
    assert len(found) == 1 and gliner["model"].predicted_on == ["cpu"]
    assert gliner["model"].moves == ["cpu"] and holding() == [] and waiting() == []


@pytest.mark.asyncio
async def test_an_owner_request_mid_pass_sends_gliner_back_and_the_pass_finishes_on_the_cpu(gliner):
    base_mod.bind_owner_loop(asyncio.get_running_loop())
    await asyncio.wait_for(asyncio.to_thread(kg._get_gliner_model, "cuda"), BOUND)
    w = whisper()
    queued = threading.Event()
    loop = asyncio.get_running_loop()
    dictation = {}

    async def dictate():
        async with w.lease.use("dictation", wait_s=BOUND):
            dictation["holding"] = holding()

    def after_first():
        # An owner's dictation arrives while the first text is on the card.
        dictation["task"] = asyncio.run_coroutine_threadsafe(dictate(), loop)
        deadline = threading.Event()
        for _ in range(500):
            if waiting() == ["whisper:turbo"]:
                break
            deadline.wait(0.01)
        queued.set()

    gliner["model"].after_first = after_first
    found = await asyncio.wait_for(asyncio.to_thread(extract, "cuda", texts(30)), BOUND)
    assert queued.is_set() and len(found) == 30
    await asyncio.wait_for(asyncio.wrap_future(dictation["task"]), BOUND)
    assert dictation["holding"] == ["whisper:turbo"]
    assert gliner["model"].predicted_on[0] == "cuda"
    assert set(gliner["model"].predicted_on[1:]) == {"cpu"}
    assert gliner["model"].moves == ["cpu", "cuda", "cpu"]
    await nothing_left()
