"""Active Recall leaves the event loop serving, waits a bounded time, and loses one channel at a time.

Board entry AN-AGENT-WAKING-ON-A-MENTION-HOLDS-THE-EVENT-LOOP-FOR-TENS-OF-SECONDS (V3 of
the 2026-11-07 outside review): the recall block ran synchronously on the backend's
event loop, so the node served nobody while an agent built its context (14.8 s and 53 s
measured); it decided "skip" after every search had run; and one `try` dropped BM25
together with a failed vector channel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import threading
import time
from collections import OrderedDict

import pytest

from dpc_client_core.dpc_agent import active_recall as recall_mod
from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as context_mod
from dpc_client_core.dpc_agent import memory as memory_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.providers.base import ModelNotCachedError

PROFILE = "agent_x"
QUERY = "harbour inventory ledger"
MARKS = ["OWN-MARK-ALPHA-11", "OWN-MARK-BRAVO-22", "OWN-MARK-CHARLIE-33"]
# BM25 gives a word in every document no weight; unrelated rows keep the query rare.
FILLERS = [{"source_file": f"knowledge/filler-{i}.md", "source_layer": "L5",
            "heading": f"Filler {i}", "text": f"unrelated weather almanac page {i}"}
           for i in range(6)]
_REAL_BACKEND = retrieval_mod.make_backend_for_agent


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))

    agent_root = tmp_path / "agents" / PROFILE
    (agent_root / "knowledge").mkdir(parents=True)
    Memory(agent_root).ensure_files()
    metas = []
    for i, mark in enumerate(MARKS):
        f = agent_root / "knowledge" / f"note-{i}.md"
        f.write_text(f"# Note {i}\n{QUERY} {mark}", encoding="utf-8")
        metas.append({"source_file": f"knowledge/{f.name}", "source_layer": "L5",
                      "source_path": str(f), "heading": f"Note {i}",
                      "text": f.read_text(encoding="utf-8")})
    backend = _REAL_BACKEND(agent_root, dimensions=8)
    backend.text.add([TextAddItem(text=m["text"], meta=m) for m in metas + FILLERS])
    backend.text.save()

    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {PROFILE: {"memory": {"enabled": True, "active_recall": True}}},
    }), encoding="utf-8")
    # A clean offer map, so "nothing was offered" is about this test's turn.
    monkeypatch.setattr(recall_mod, "_OFFERED_ADDRESSES", OrderedDict())
    return {"agent_root": agent_root, "firewall": ContextFirewall(rules)}


class _Vector:
    """A vector channel that is present and current, so a turn encodes its query."""

    def __init__(self, load_error=None):
        self.load_error = load_error

    def load(self):
        if self.load_error:
            raise self.load_error
        return True

    def needs_rebuild(self, *a, **kw):
        return False

    def search(self, vec, top_k):
        return []


class _Provider:
    model_name = "test/model"
    dimensions = 4
    max_tokens = 4096

    def __init__(self, on_embed=None):
        self.on_embed = on_embed
        self.embedded = []

    def embed(self, text):
        self.embedded.append(text)
        if self.on_embed:
            self.on_embed(text)
        return [0.1] * self.dimensions


@pytest.fixture
def stage(world, monkeypatch):
    """The real text index behind a stub vector channel; counts opens, marks the fuse."""
    s = {"opened": [], "fused": threading.Event(), "vector": _Vector()}

    def _backend(root, **kw):
        s["opened"].append(root)
        backend = _REAL_BACKEND(root, dimensions=8)
        backend.vector = s["vector"]
        real_fuse = backend.fuser.fuse

        def fuse(*a, **kw):
            try:
                return real_fuse(*a, **kw)
            finally:
                s["fused"].set()

        backend.fuser.fuse = fuse
        return backend

    def _no_fallback(*a, **kw):
        raise AssertionError("recall asked for an embedding provider")

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _backend)
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)
    monkeypatch.setattr(memory_mod, "get_embedding_provider", _no_fallback)
    return s


class _PromptBuilt(Exception):
    pass


def _agent(w, provider):
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = w["agent_root"]
    agent.memory = Memory(w["agent_root"])
    agent.skill_store = None
    agent._embedding_provider = provider
    agent._firewall = w["firewall"]
    agent._firewall_profile = PROFILE
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    return agent


async def _turn(w, monkeypatch, provider, session_state=None) -> str:
    """The agent's own process() up to the prompt, through the real builder; the tail."""
    real_build = context_mod.build_llm_messages
    seen = {}

    def _build(**kwargs):
        _messages, cap = real_build(**kwargs)
        seen["tail"] = cap.get("turn_context") or ""
        raise _PromptBuilt

    monkeypatch.setattr(agent_mod, "build_llm_messages", _build)
    with pytest.raises(_PromptBuilt):
        await _agent(w, provider).process(QUERY, "conv-1", session_state=session_state,
                                          message_source="ui")
    return seen["tail"]


def _marks(tail):
    return [m for m in MARKS if m in tail]


def _access_log(w):
    return w["agent_root"] / "state" / "knowledge_access.jsonl"


# --- off the loop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_loop_keeps_serving_while_a_turn_encodes(world, stage, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=context_mod.__name__)
    provider = _Provider(on_embed=lambda text: time.sleep(0.4))
    ticks = []
    done = asyncio.Event()

    async def ticker():
        while not done.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)
    tail = await _turn(world, monkeypatch, provider)
    # The turn's end counts as a tick: a blocked loop leaves its gap just before it.
    ticks.append(time.monotonic())
    done.set()
    await task

    assert provider.embedded == [QUERY]
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.3, f"the loop stood still for {max(gaps):.2f} s while a query was encoded"
    # Within the bound the hints arrive and are recorded as usual — the late-result
    # test below asserts the absence of exactly these records.
    assert _marks(tail) == MARKS
    assert len(_access_log(world).read_text(encoding="utf-8").splitlines()) == 1
    assert "conv-1" in recall_mod._OFFERED_ADDRESSES
    timing = [r.getMessage() for r in caplog.records if "Active Recall timings" in r.getMessage()]
    assert len(timing) == 1, timing
    for step in ("open=", "load=", "encode=", "search=", "bm25=", "graph=", "fuse="):
        assert step in timing[0], timing[0]


@pytest.mark.asyncio
async def test_past_the_bound_the_turn_goes_on_and_the_late_result_records_nothing(
        world, stage, monkeypatch):
    monkeypatch.setattr(context_mod, "_RECALL_WAIT_SEC", 0.2, raising=False)
    release = threading.Event()
    provider = _Provider(on_embed=lambda text: release.wait(timeout=3.0))

    started = time.monotonic()
    tail = await _turn(world, monkeypatch, provider)
    waited = time.monotonic() - started

    assert _marks(tail) == [], "hints reached a turn that had stopped waiting for them"
    assert waited < 2.0, f"the turn waited {waited:.1f} s past a 0.2 s bound"

    release.set()
    assert stage["fused"].wait(timeout=5.0), "the abandoned run never finished"
    await asyncio.sleep(0.3)  # the worker's last lines after the fuse

    assert not _access_log(world).exists(), "a late result was logged as an injection"
    assert "conv-1" not in recall_mod._OFFERED_ADDRESSES, "a late result was recorded as an offer"
    assert recall_mod.followed_a_hint("conv-1", str(world["agent_root"] / "knowledge" / "note-0.md")) is None


# --- skip before any search -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_skip_turn_never_opens_the_index_or_encodes(world, stage, monkeypatch):
    provider = _Provider()

    tail = await _turn(world, monkeypatch, provider,
                       session_state={"context_usage_percent": 85})

    assert _marks(tail) == []
    assert stage["opened"] == [], "a skip turn opened the index"
    assert provider.embedded == [], "a skip turn encoded a query"


def test_a_skip_turn_through_the_builder_itself_does_no_recall_work(world, stage):
    provider = _Provider()

    _messages, cap = context_mod.build_llm_messages(
        agent_root=world["agent_root"], memory=Memory(world["agent_root"]),
        task={"id": "t1", "type": "chat", "text": QUERY},
        session_state={"context_usage_percent": 85}, embedding_provider=provider,
    )

    assert _marks(cap.get("turn_context") or "") == []
    assert stage["opened"] == []
    assert provider.embedded == []


@pytest.mark.asyncio
async def test_a_hints_turn_still_runs_below_the_skip_line(world, stage, monkeypatch):
    """The early decision moves skip, not the other modes."""
    tail = await _turn(world, monkeypatch, _Provider(),
                       session_state={"context_usage_percent": 60})

    assert "RECALL HINTS" in tail
    assert stage["opened"]


# --- one channel fails, the others stay ----------------------------------------


def _raise(exc):
    def _on_embed(text):
        raise exc
    return _on_embed


@pytest.mark.asyncio
@pytest.mark.parametrize("exc, level", [
    (ModelNotCachedError("test/model", "/nowhere"), logging.INFO),
    (RuntimeError("the encoder broke"), logging.WARNING),
], ids=["model-not-cached", "generic"])
async def test_a_failed_encoder_keeps_the_word_channel(world, stage, monkeypatch, caplog, exc, level):
    caplog.set_level(logging.INFO, logger=context_mod.__name__)
    provider = _Provider(on_embed=_raise(exc))

    tail = await _turn(world, monkeypatch, provider)

    assert provider.embedded, "the vector channel was not reached"
    assert _marks(tail) == MARKS
    assert any(r.levelno == level and "vector" in r.getMessage() for r in caplog.records), \
        [(r.levelname, r.getMessage()) for r in caplog.records]


@pytest.mark.asyncio
async def test_a_vector_index_that_fails_to_load_keeps_the_word_channel(world, stage, monkeypatch):
    stage["vector"] = _Vector(load_error=RuntimeError("torn index file"))

    tail = await _turn(world, monkeypatch, _Provider())

    assert _marks(tail) == MARKS


@pytest.mark.asyncio
async def test_a_failed_graph_keeps_both_other_channels(world, stage, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=context_mod.__name__)

    class _BrokenGraph:
        def graph_expand(self, *a, **kw):
            raise RuntimeError("graph store broke")

    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: _BrokenGraph())

    tail = await _turn(world, monkeypatch, _Provider())

    assert _marks(tail) == MARKS
    assert any(r.levelno == logging.WARNING and "Graph" in r.getMessage() for r in caplog.records)
