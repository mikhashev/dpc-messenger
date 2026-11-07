"""The memory switches of an agent's profile decide recall and memory_search, live.

Board entry THE-ACTIVE-RECALL-SETTINGS-ON-SCREEN-AND-IN-CONFIG-SWITCH-NOTHING: the
"Enable Memory System" and "Active Recall" boxes wrote `agent_profiles.<id>.memory`
and nothing on the per-turn path read it, `max_recall_results` was read nowhere, and a
bad value saved without a word. The block is now read from the firewall the agent holds
on every turn (Mike's call, 2026-11-07), and validated at save.

Also the first step of MEMORY-SEARCH-GIVES-THE-AGENT-A-KEY-AND-A-SCORE-AND-IGNORES-THE-
MEMORY-SWITCH-AND-REVOKED-PATHS: memory_search honours the switch, drops rows of a
revoked path, and leaves a stale vector channel out.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as context_mod
from dpc_client_core.dpc_agent import memory as memory_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem, VectorAddItem
from dpc_client_core.dpc_agent.tools.core import memory_search
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.providers import model_sizes

PROFILE = "agent_x"
QUERY = "harbour inventory ledger"
MARKS = ["OWN-MARK-ALPHA-11", "OWN-MARK-BRAVO-22", "OWN-MARK-CHARLIE-33"]
SECRET_MARK = "SECRET-MARK-41d2"
OPEN_MARK = "OPEN-MARK-8a07"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))

    agent_root = tmp_path / "agents" / PROFILE
    (agent_root / "knowledge").mkdir(parents=True)
    Memory(agent_root).ensure_files()

    own = []
    for i, mark in enumerate(MARKS):
        f = agent_root / "knowledge" / f"note-{i}.md"
        f.write_text(f"# Note {i}\n{QUERY} {mark}", encoding="utf-8")
        own.append(f)

    secret_root = tmp_path / "projects" / "secret-project"
    open_root = tmp_path / "projects" / "open-project"
    for root in (secret_root, open_root):
        root.mkdir(parents=True)
    secret = secret_root / "plan.md"
    opened = open_root / "notes.md"
    secret.write_text(f"# Secret Plan\n{QUERY} {SECRET_MARK}", encoding="utf-8")
    opened.write_text(f"# Open Notes\n{QUERY} {OPEN_MARK}", encoding="utf-8")

    w = {"agent_root": agent_root, "rules": tmp_path / "privacy_rules.json", "own": own,
         "secret_root": secret_root, "open_root": open_root,
         "secret": secret, "open": opened}
    write_rules(w, {"enabled": True, "active_recall": True})
    w["firewall"] = ContextFirewall(w["rules"])
    return w


def write_rules(w, memory, read_only=None, profile=PROFILE):
    """A rules file with `memory` as the profile's block (None: no block at all).

    The global dpc_agent block has memory on, so a test that sees no hint knows the
    profile decided, not a missing switch somewhere."""
    if read_only is None:
        read_only = [w["secret_root"], w["open_root"]]
    prof = {"sandbox_extensions": {"read_only": [str(p) for p in read_only],
                                   "extended_read_enabled": True}}
    if memory is not None:
        prof["memory"] = memory
    w["rules"].write_text(json.dumps({
        "dpc_agent": {"enabled": True, "memory": {"enabled": True, "active_recall": True}},
        "agent_profiles": {profile: prof} if profile else {},
    }), encoding="utf-8")


def save(w, memory, **kw):
    """What a firewall save does: the file changes and the live object reloads."""
    write_rules(w, memory, **kw)
    ok, msg = w["firewall"].reload()
    assert ok, msg


def _meta(key, layer, path):
    return {"source_file": key, "source_layer": layer, "source_path": str(path),
            "heading": path.read_text(encoding="utf-8").splitlines()[0][2:],
            "text": path.read_text(encoding="utf-8")}


def own_metas(w):
    return [_meta(f"knowledge/{p.name}", "L5", p) for p in w["own"]]


def ext_metas(w):
    return [_meta("EXT/secret-project/plan.md", "EXT", w["secret"]),
            _meta("EXT/open-project/notes.md", "EXT", w["open"])]


_REAL_BACKEND = retrieval_mod.make_backend_for_agent

# BM25 gives a word in every document no weight; unrelated rows keep the query
# words rare enough to score.
FILLERS = [{"source_file": f"knowledge/filler-{i}.md", "source_layer": "L5",
            "heading": f"Filler {i}", "text": f"unrelated weather almanac page {i}"}
           for i in range(6)]


def index_text(w, metas):
    """Written through the real factory, so a spy on it counts only the code under test."""
    backend = _REAL_BACKEND(w["agent_root"], dimensions=8)
    backend.text.add([TextAddItem(text=m["text"], meta=m) for m in metas + FILLERS])
    backend.text.save()


class _Provider:
    """An embedding provider that counts what it is asked to encode."""

    def __init__(self, model_name="test/model", dims=4, max_tokens=4096):
        self.model_name = model_name
        self.dimensions = dims
        self.max_tokens = max_tokens
        self.embedded = []

    def embed(self, text):
        self.embedded.append(text)
        return [0.1] * self.dimensions


def _agent(w, profile=PROFILE, provider=None):
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = w["agent_root"]
    agent.memory = Memory(w["agent_root"])
    agent.skill_store = None
    agent._embedding_provider = provider
    agent._firewall = w["firewall"]
    agent._firewall_profile = profile
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    return agent


class _PromptBuilt(Exception):
    pass


@pytest.fixture
def turn(world, monkeypatch):
    """Run the agent's own process() up to the prompt, through the real
    build_llm_messages, and return the tail plus how often recall opened the index."""
    real_build = context_mod.build_llm_messages
    real_backend = _REAL_BACKEND
    opened = []

    def _backend(root, **kw):
        opened.append(root)
        return real_backend(root, dimensions=8)

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _backend)
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)

    def _no_fallback(*a, **kw):
        raise AssertionError("recall asked for an embedding provider")

    monkeypatch.setattr(memory_mod, "get_embedding_provider", _no_fallback)

    async def run(profile=PROFILE, provider=None):
        seen = {}

        def _build(**kwargs):
            _messages, cap = real_build(**kwargs)
            seen["tail"] = cap.get("turn_context") or ""
            raise _PromptBuilt

        monkeypatch.setattr(agent_mod, "build_llm_messages", _build)
        with pytest.raises(_PromptBuilt):
            await _agent(world, profile, provider).process(QUERY, "conv-1", message_source="ui")
        return seen["tail"]

    run.opened = opened
    return run


def marks_in(tail):
    return [m for m in MARKS if m in tail]


# --- recall, per turn ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_harness_carries_hints_while_memory_is_on(world, turn):
    index_text(world, own_metas(world))
    tail = await turn()
    assert marks_in(tail) == MARKS


@pytest.mark.asyncio
async def test_memory_off_does_no_recall_work_at_all(world, turn):
    index_text(world, own_metas(world))
    save(world, {"enabled": False, "active_recall": True})
    provider = _Provider()

    tail = await turn(provider=provider)

    assert marks_in(tail) == []
    assert turn.opened == [], "the index was opened with memory off"
    assert provider.embedded == [], "a query was encoded with memory off"


@pytest.mark.asyncio
async def test_active_recall_off_gives_no_hints(world, turn):
    index_text(world, own_metas(world))
    save(world, {"enabled": True, "active_recall": False})
    provider = _Provider()

    tail = await turn(provider=provider)

    assert marks_in(tail) == []
    assert turn.opened == []
    assert provider.embedded == []


@pytest.mark.asyncio
async def test_a_save_applies_to_the_next_turn_without_a_restart(world, turn):
    index_text(world, own_metas(world))
    assert marks_in(await turn()) == MARKS

    save(world, {"enabled": True, "active_recall": False})
    assert marks_in(await turn()) == []

    save(world, {"enabled": False, "active_recall": True})
    assert marks_in(await turn()) == []

    save(world, {"enabled": True, "active_recall": True})
    assert marks_in(await turn()) == MARKS


@pytest.mark.asyncio
async def test_max_recall_results_caps_the_hints(world, turn):
    index_text(world, own_metas(world))
    save(world, {"enabled": True, "active_recall": True, "max_recall_results": 1})

    tail = await turn()

    assert len(marks_in(tail)) == 1, tail


@pytest.mark.asyncio
async def test_an_agent_without_its_own_profile_gets_no_hints(world, turn):
    """The global dpc_agent block has memory on; it is not asked."""
    index_text(world, own_metas(world))

    assert marks_in(await turn(profile="agent_without_profile")) == []
    assert marks_in(await turn(profile=None)) == []
    assert turn.opened == []


@pytest.mark.asyncio
async def test_a_profile_without_a_memory_block_keeps_the_code_default_off(world, turn):
    index_text(world, own_metas(world))
    save(world, None)

    assert marks_in(await turn()) == []


def test_old_callers_that_pass_nothing_keep_recall(world, monkeypatch):
    """build_llm_messages called without the new arguments behaves as before."""
    index_text(world, own_metas(world))
    real_backend = _REAL_BACKEND
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=8))
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)

    _messages, cap = context_mod.build_llm_messages(
        agent_root=world["agent_root"], memory=Memory(world["agent_root"]),
        task={"id": "t1", "type": "chat", "text": QUERY},
    )

    assert marks_in(cap.get("turn_context") or "") == MARKS


# --- memory_search ------------------------------------------------------------


def _ctx(w, profile=PROFILE):
    ctx = ToolContext(agent_root=w["agent_root"], firewall=w["firewall"], message_source="ui")
    ctx._agent = _agent(w, profile)
    return ctx


@pytest.fixture
def search_env(world, monkeypatch):
    """memory_search with no embedding model: the word channel alone answers."""
    real_backend = _REAL_BACKEND
    opened = []

    def _backend(root, **kw):
        opened.append(root)
        return real_backend(root, dimensions=4)

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _backend)
    provider = _Provider()
    monkeypatch.setattr(memory_mod, "get_embedding_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: False)
    return {"opened": opened, "provider": provider}


def test_memory_search_with_memory_off_refuses_and_opens_nothing(world, search_env):
    index_text(world, own_metas(world))
    save(world, {"enabled": False})

    answer = memory_search(_ctx(world), QUERY)

    assert answer == "Memory is off for this agent."
    assert search_env["opened"] == []


def test_memory_search_with_only_active_recall_off_still_answers(world, search_env):
    index_text(world, own_metas(world))
    save(world, {"enabled": True, "active_recall": False})

    answer = memory_search(_ctx(world), QUERY)

    assert "knowledge/note-0.md" in answer


def test_memory_search_for_an_agent_without_a_profile_refuses(world, search_env):
    index_text(world, own_metas(world))

    assert memory_search(_ctx(world, "agent_without_profile"), QUERY) == \
        "Memory is off for this agent."


def test_memory_search_without_a_firewall_behaves_as_before(world, search_env):
    index_text(world, own_metas(world) + ext_metas(world))
    ctx = ToolContext(agent_root=world["agent_root"], firewall=None, message_source="ui")

    answer = memory_search(ctx, QUERY, top_k=10)

    assert "EXT/secret-project/plan.md" in answer
    assert "knowledge/note-0.md" in answer


def test_memory_search_drops_a_revoked_external_row(world, search_env):
    index_text(world, own_metas(world) + ext_metas(world))
    before = memory_search(_ctx(world), QUERY, top_k=10)
    assert "EXT/secret-project/plan.md" in before

    save(world, {"enabled": True}, read_only=[world["open_root"]])
    after = memory_search(_ctx(world), QUERY, top_k=10)

    assert "EXT/secret-project/plan.md" not in after
    assert "EXT/open-project/notes.md" in after
    assert "knowledge/note-0.md" in after
    assert after.startswith("Found 4 results")


def test_memory_search_drops_an_external_row_with_no_path(world, search_env):
    no_path = {"source_file": "EXT/old/row.md", "source_layer": "EXT",
               "heading": "Old Row", "text": f"{QUERY} old row body"}
    index_text(world, [no_path] + own_metas(world))

    answer = memory_search(_ctx(world), QUERY, top_k=10)

    assert "EXT/old/row.md" not in answer
    assert "knowledge/note-0.md" in answer


def test_memory_search_on_a_stale_vector_index_answers_with_words_only(world, monkeypatch):
    pytest.importorskip("faiss")
    real_backend = _REAL_BACKEND
    # Vectors written by another model than the one now configured.
    old = real_backend(world["agent_root"], dimensions=4, model_name="test/old-model")
    vec_meta = _meta("knowledge/vector-only.md", "L5", world["own"][0])
    old.vector.add([VectorAddItem(vector=np.ones(4, dtype=np.float32), meta=vec_meta)])
    old.vector.save()
    index_text(world, own_metas(world))

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=4))
    provider = _Provider(model_name="test/new-model", dims=4)
    monkeypatch.setattr(memory_mod, "get_embedding_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: True)

    answer = memory_search(_ctx(world), QUERY, top_k=10)

    lines = answer.splitlines()
    assert lines[0] == ("Vector index is stale for the current embedding model/window; "
                        "word matches only.")
    assert "knowledge/note-0.md" in answer
    assert "knowledge/vector-only.md" not in answer
    assert provider.embedded == [], "a query was encoded against a stale index"


def test_memory_search_on_a_current_vector_index_has_no_stale_line(world, monkeypatch):
    pytest.importorskip("faiss")
    real_backend = _REAL_BACKEND
    cur = real_backend(world["agent_root"], dimensions=4, model_name="test/model")
    cur.vector.add([VectorAddItem(vector=np.ones(4, dtype=np.float32),
                                  meta=_meta("knowledge/note-0.md", "L5", world["own"][0]))])
    cur.vector._inner._header.max_tokens = 4096
    cur.vector.save()
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=4))
    provider = _Provider(model_name="test/model", dims=4)
    monkeypatch.setattr(memory_mod, "get_embedding_provider", lambda *a, **kw: provider)
    monkeypatch.setattr(model_sizes, "is_model_cached", lambda name: True)

    answer = memory_search(_ctx(world), QUERY)

    assert "stale" not in answer
    assert provider.embedded == [QUERY]
    assert "knowledge/note-0.md" in answer


# --- validation at save -------------------------------------------------------


def _rules_with(memory):
    return {"agent_profiles": {PROFILE: {"memory": memory}}}


@pytest.mark.parametrize("bad", [0, 11, "3", 2.0, True, None])
def test_validate_refuses_a_bad_max_recall_results(bad):
    ok, errors = ContextFirewall.validate_config(_rules_with({"max_recall_results": bad}))
    assert not ok
    assert any("max_recall_results" in e and "1 to 10" in e for e in errors), errors


@pytest.mark.parametrize("field", ["enabled", "active_recall"])
@pytest.mark.parametrize("bad", ["true", 1, None])
def test_validate_refuses_a_non_boolean_switch(field, bad):
    ok, errors = ContextFirewall.validate_config(_rules_with({field: bad}))
    assert not ok
    assert any(f"memory.{field}" in e and "boolean" in e for e in errors), errors


def test_validate_refuses_a_memory_block_that_is_not_a_dictionary():
    ok, errors = ContextFirewall.validate_config(_rules_with([True]))
    assert not ok
    assert any("memory' must be a dictionary" in e for e in errors), errors


@pytest.mark.parametrize("n", [1, 3, 10])
def test_validate_accepts_a_valid_block_and_tolerates_unknown_keys(n):
    ok, errors = ContextFirewall.validate_config(_rules_with({
        "enabled": True, "active_recall": False, "max_recall_results": n,
        "embedding_device": "cpu", "some_future_key": {"x": 1},
    }))
    assert ok, errors


def test_a_file_that_already_holds_a_valid_block_still_loads(world):
    """The shape the UI writes today, read back through reload()."""
    write_rules(world, {"enabled": True, "active_recall": True, "embedding_device": "cpu"})
    ok, msg = world["firewall"].reload()
    assert ok, msg


def test_a_bad_save_is_refused_and_leaves_the_file_alone(world):
    before = world["rules"].read_text(encoding="utf-8")
    rules = json.loads(before)
    rules["agent_profiles"][PROFILE]["memory"]["max_recall_results"] = 0

    ok, _msg, errors = world["firewall"].save_rules_from_dict(rules)

    assert not ok
    assert any("max_recall_results" in e for e in errors), errors
    assert world["rules"].read_text(encoding="utf-8") == before
