"""A turn sees only the memory layers its audience may see (ADR-048, steps 2).

A group turn counted as the owner's whatever its members, so an agent in a room with
another person's node offered that room the owner's own notes, scratchpad and granted
files (A-GROUP-TURN-CAN-SHOW-THE-OWNERS-PRIVATE-MEMORY-TO-OTHER-NODES). Mike's call,
2026-11-08 (review by Zcode, Johnny, Ark): each layer is marked private or shared in
`memory.layer_audience`; the owner's doors and a group of this node and its own nodes
(`memory.own_node_group`) see every layer, any other group only the shared ones, a
stranger none. Recall, memory_search, the prompt sections and the replayed tails all
follow the same set.
"""

from __future__ import annotations

import json
import logging
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as context_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.active_recall import get_recall_block, layer_of
from dpc_client_core.dpc_agent.agent import (
    ALL_MEMORY_LAYERS, OTHER_NODE_TURN, OWNER_TURN, STRANGER_TURN, DpcAgent,
    layer_audience, open_layers, turn_class,
)
from dpc_client_core.dpc_agent.context import build_llm_messages, strip_closed_layers
from dpc_client_core.dpc_agent.hybrid_search import SearchResult
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem
from dpc_client_core.dpc_agent.tools.core import get_dpc_context, memory_search
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall

PROFILE = "agent_x"
THIS = "dpc-node-" + "a" * 32
LINUX = "dpc-node-" + "b" * 32
OTHER = "dpc-node-" + "c" * 32
QUERY = "harbour inventory ledger"
OWN_MARK = "OWN-BODY-MARK-31aa"
SHARED_MARK = "SHARED-BODY-MARK-77de"
SCRATCH_MARK = "SCRATCH-MARK-0b9f"


def _group(members, discord=False, gid="group-x"):
    return SimpleNamespace(group_id=gid, name=gid, members=list(members),
                           is_discord_bridge=discord)


class _Groups:
    def __init__(self, *groups):
        self.node_id = THIS
        self._g = {g.group_id: g for g in groups}

    def get_group(self, gid):
        return self._g.get(gid)

    def get_all_groups(self):
        return list(self._g.values())


def _rules(tmp_path, memory=None, node_groups=None):
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "node_groups": node_groups if node_groups is not None else {"home-boxes": [LINUX]},
        "agent_profiles": {PROFILE: {
            "human_knowledge_access": True,
            "personal_context_access": True,
            "memory": {"enabled": True, "active_recall": True, **(memory or {})},
        }},
    }), encoding="utf-8")
    return ContextFirewall(rules)


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))
    root = tmp_path / "agents" / PROFILE
    (root / "knowledge").mkdir(parents=True)
    Memory(root).ensure_files()
    (root / "memory" / "scratchpad.md").write_text(f"notes {SCRATCH_MARK}", encoding="utf-8")
    own = root / "knowledge" / "own-note.md"
    own.write_text(f"# Own Note\n{QUERY} {OWN_MARK}", encoding="utf-8")
    shared = home / ".dpc" / "knowledge" / "shared-note.md"
    shared.parent.mkdir(parents=True)
    shared.write_text(f"# Shared Note\n{QUERY} {SHARED_MARK}", encoding="utf-8")
    return SimpleNamespace(root=root, own=own, shared=shared, home=home / ".dpc", tmp=tmp_path)


def _agent(w, firewall, groups=None):
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = w.root
    agent.memory = Memory(w.root)
    agent.skill_store = agent._embedding_provider = None
    agent._firewall = firewall
    agent._firewall_profile = PROFILE
    agent._service = SimpleNamespace(group_manager=groups or _Groups())
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    return agent


def _index(w):
    backend = retrieval_mod.make_backend_for_agent(w.root, dimensions=8)
    items = []
    for path, key, layer, heading in ((w.own, "knowledge/own-note.md", "L5", "Own Note"),
                                      (w.shared, "L6/shared-note.md", "L6", "Shared Note")):
        text = path.read_text(encoding="utf-8")
        items.append(TextAddItem(text=text, meta={
            "source_file": key, "source_layer": layer, "source_path": str(path),
            "heading": heading, "text": text}))
    backend.text.add(items)
    backend.text.save()


# -- the class comes from the members -----------------------------------------


@pytest.mark.parametrize("members", [[THIS], [THIS, LINUX]])
def test_a_group_of_this_node_and_its_own_nodes_is_the_owner(members):
    assert turn_class("group", _group(members), THIS, {LINUX}) == OWNER_TURN


def test_a_group_with_another_node_sees_only_the_shared_layers():
    cls = turn_class("group", _group([THIS, LINUX, OTHER]), THIS, {LINUX})
    assert cls == OTHER_NODE_TURN
    assert open_layers(cls, layer_audience({"layer_audience": {"L6": "shared"}})) == frozenset({"L6"})


def test_with_default_marks_a_group_with_another_node_opens_no_layer():
    # L6 became private by default on 2026-11-09 (Mike's call, review by Zcode, Johnny).
    cls = turn_class("group", _group([THIS, LINUX, OTHER]), THIS, {LINUX})
    assert cls == OTHER_NODE_TURN
    assert layer_audience({}) == {"L5": "private", "L6": "private", "EXT": "private"}
    assert open_layers(cls, layer_audience({})) == frozenset()
    assert open_layers(cls) == frozenset()


def test_a_discord_bridge_is_never_the_owner_even_with_only_this_node():
    assert turn_class("group", _group([THIS], discord=True), THIS, set()) == OTHER_NODE_TURN


@pytest.mark.parametrize("group", [None, _group([]), _group([], gid="telegram-123")],
                         ids=["missing", "empty roster", "telegram-*"])
def test_a_missing_group_or_an_empty_roster_is_never_own(group):
    cls = turn_class("group", group, THIS, {LINUX})
    assert cls != OWNER_TURN
    assert "L5" not in open_layers(cls, layer_audience({}))


@pytest.mark.parametrize("source", ["ui", "external_agent", "telegram_owner"])
def test_the_owner_s_doors_see_every_layer_whatever_the_marks(source):
    marks = layer_audience({"layer_audience": {"L5": "private", "L6": "private", "EXT": "private"}})
    assert open_layers(turn_class(source), marks) == ALL_MEMORY_LAYERS


@pytest.mark.parametrize("source", ["telegram", "discord", None, "telegram_public", "elsewhere"])
def test_a_stranger_sees_no_layer_even_when_all_are_shared(source):
    marks = layer_audience({"layer_audience": {"L5": "shared", "L6": "shared", "EXT": "shared"}})
    assert turn_class(source) == STRANGER_TURN
    assert open_layers(turn_class(source), marks) == frozenset()


def test_marking_l5_shared_brings_it_back_to_a_mixed_group():
    marks = layer_audience({"layer_audience": {"L5": "shared"}})
    assert open_layers(OTHER_NODE_TURN, marks) == frozenset({"L5"})


def test_a_bad_mark_in_memory_keeps_the_default():
    marks = layer_audience({"layer_audience": {"L5": "everyone", "L9": "shared"}})
    assert marks == {"L5": "private", "L6": "private", "EXT": "private"}


def test_the_agent_reads_the_own_group_from_its_profile(world, tmp_path):
    fw = _rules(tmp_path, memory={"own_node_group": "home-boxes"})
    groups = _Groups(_group([THIS, LINUX], gid="group-own"),
                     _group([THIS, OTHER], gid="group-mixed"))
    agent = _agent(world, fw, groups)
    assert agent.turn_audience("group", "group-own") == (OWNER_TURN, ALL_MEMORY_LAYERS)
    assert agent.turn_audience("group", "group-mixed") == (OTHER_NODE_TURN, frozenset())
    assert agent.turn_audience("group", "group-gone")[0] == OTHER_NODE_TURN
    assert agent.turn_audience("telegram", "telegram-5") == (STRANGER_TURN, frozenset())


def test_without_an_own_group_a_group_with_the_linux_node_is_not_the_owner(world, tmp_path):
    agent = _agent(world, _rules(tmp_path), _Groups(_group([THIS, LINUX], gid="group-own")))
    assert agent.turn_audience("group", "group-own")[0] == OTHER_NODE_TURN


# -- validation ---------------------------------------------------------------


def _profile_memory(memory, node_groups=None):
    return {"node_groups": node_groups if node_groups is not None else {"home-boxes": [LINUX]},
            "agent_profiles": {PROFILE: {"memory": memory}}}


@pytest.mark.parametrize("memory", [
    {"layer_audience": ["L5"]},
    {"layer_audience": {"L7": "shared"}},
    {"layer_audience": {"L5": "personal"}},
    {"own_node_group": "home_boxes"},
    {"own_node_group": 5},
])
def test_save_refuses_a_bad_mark_or_an_own_group_not_in_node_groups(memory):
    ok, errors = ContextFirewall.validate_config(_profile_memory(memory))
    assert not ok and errors, memory


def test_save_takes_good_marks_and_a_known_own_group():
    ok, errors = ContextFirewall.validate_config(_profile_memory(
        {"layer_audience": {"L5": "shared", "L6": "private", "EXT": "private"},
         "own_node_group": "home-boxes"}))
    assert ok, errors


# -- the startup line ---------------------------------------------------------


def test_startup_names_the_own_group_and_how_each_chat_group_resolves(world, tmp_path, caplog):
    fw = _rules(tmp_path, memory={"own_node_group": "home-boxes"})
    groups = _Groups(_group([THIS, LINUX], gid="group-own"),
                     _group([THIS, OTHER], gid="group-mixed"),
                     _group([THIS], discord=True, gid="group-discord"))
    agent = _agent(world, fw, groups)
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.agent"):
        agent._log_own_nodes()
    lines = [r.getMessage() for r in caplog.records if "layer audience" in r.getMessage()]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "own node group 'home-boxes' (1 nodes)" in line
    assert "resolved as own: group-own" in line
    assert "group-mixed" in line.split("not own:")[1]
    assert "Discord bridge" in line


def test_startup_says_why_there_are_no_own_nodes(world, tmp_path, caplog):
    fw = _rules(tmp_path, memory={"own_node_group": "home-boxes"}, node_groups={})
    agent = _agent(world, fw, _Groups())
    with caplog.at_level(logging.INFO, logger="dpc_client_core.dpc_agent.agent"):
        agent._log_own_nodes()
    assert any("'home-boxes' is not in node_groups" in r.getMessage() for r in caplog.records)


# -- Active Recall ------------------------------------------------------------


def _row(key, layer, path=None):
    return SearchResult(chunk_meta={"source_file": key, "source_layer": layer,
                                    "source_path": path or "", "text": f"text of {key}"},
                        score=1.0, source="fused")


ROWS = [_row("knowledge/a.md", "L5"), _row("L6/b.md", "L6", "/x/b.md"),
        _row("EXT/proj/c.md", "EXT", "/x/c.md"), _row("knowledge/d.md", "L7"),
        _row("nokey", "L5")]


def test_a_mixed_group_s_recall_keeps_only_l6_rows_and_a_graph_hit_takes_its_key_s_layer():
    block = get_recall_block(ROWS, max_results=10, open_layers=frozenset({"L6"}))
    assert [r.chunk_meta["source_file"] for r in block.injected] == ["L6/b.md"]


def test_the_owner_s_recall_keeps_every_row_as_before():
    block = get_recall_block(ROWS, max_results=10, open_layers=ALL_MEMORY_LAYERS)
    assert len(block.injected) == len(ROWS)
    assert layer_of({"source_file": "knowledge/d.md", "source_layer": "L7"}) == "L5"


class _PromptBuilt(Exception):
    pass


async def _turn(w, monkeypatch, agent, source, conversation_id):
    real_build = context_mod.build_llm_messages
    real_backend = retrieval_mod.make_backend_for_agent
    seen = {}

    def _build(**kwargs):
        messages, cap = real_build(**kwargs)
        seen["tail"] = cap.get("turn_context") or ""
        seen["system"] = messages[0]["content"][1]["text"]
        raise _PromptBuilt

    monkeypatch.setattr(agent_mod, "build_llm_messages", _build)
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=8))
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)
    with pytest.raises(_PromptBuilt):
        await agent.process(QUERY, conversation_id, message_source=source)
    return seen


@pytest.mark.asyncio
async def test_a_mixed_group_turn_gets_l6_hints_and_no_l5_hint(world, monkeypatch, tmp_path):
    _index(world)
    fw = _rules(tmp_path, memory={"layer_audience": {"L6": "shared"}})
    agent = _agent(world, fw, _Groups(_group([THIS, OTHER], gid="group-m")))
    seen = await _turn(world, monkeypatch, agent, "group", "group-m")
    assert "Shared Note" in seen["tail"]
    assert OWN_MARK not in seen["tail"] and "Own Note" not in seen["tail"]
    assert SCRATCH_MARK not in seen["system"]


@pytest.mark.asyncio
async def test_a_mixed_group_turn_with_default_marks_opens_no_layer_and_says_why(
        world, monkeypatch, tmp_path, caplog):
    _index(world)
    agent = _agent(world, _rules(tmp_path), _Groups(_group([THIS, OTHER], gid="group-m")))
    with caplog.at_level(logging.INFO, logger=agent_mod.__name__):
        seen = await _turn(world, monkeypatch, agent, "group", "group-m")
    assert "Shared Note" not in seen["tail"] and "Own Note" not in seen["tail"]
    assert SCRATCH_MARK not in seen["system"]
    lines = [r.getMessage() for r in caplog.records if "no memory layer is open" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "group-m" in lines[0] and OTHER_NODE_TURN in lines[0] and "recall skipped" in lines[0]
    assert "L5, L6, EXT" in lines[0]


@pytest.mark.asyncio
async def test_the_owner_s_turn_logs_no_closed_layer_line(world, monkeypatch, tmp_path, caplog):
    _index(world)
    agent = _agent(world, _rules(tmp_path), _Groups())
    with caplog.at_level(logging.INFO, logger=agent_mod.__name__):
        await _turn(world, monkeypatch, agent, "ui", "conv-1")
    assert not [r for r in caplog.records if "no memory layer is open" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_group_of_own_nodes_gets_l5_back(world, monkeypatch, tmp_path):
    _index(world)
    fw = _rules(tmp_path, memory={"own_node_group": "home-boxes"})
    agent = _agent(world, fw, _Groups(_group([THIS, LINUX], gid="group-own")))
    seen = await _turn(world, monkeypatch, agent, "group", "group-own")
    assert "Own Note" in seen["tail"]
    assert SCRATCH_MARK in seen["system"]


@pytest.mark.asyncio
async def test_marking_l5_shared_brings_its_hints_back_to_a_mixed_group(world, monkeypatch, tmp_path):
    _index(world)
    fw = _rules(tmp_path, memory={"layer_audience": {"L5": "shared"}})
    agent = _agent(world, fw, _Groups(_group([THIS, OTHER], gid="group-m")))
    seen = await _turn(world, monkeypatch, agent, "group", "group-m")
    assert "Own Note" in seen["tail"]


@pytest.mark.asyncio
async def test_a_stranger_gets_no_hint_at_all(world, monkeypatch, tmp_path):
    _index(world)
    agent = _agent(world, _rules(tmp_path))
    seen = await _turn(world, monkeypatch, agent, "telegram", "telegram-5")
    assert "Shared Note" not in seen["tail"] and "Own Note" not in seen["tail"]


@pytest.mark.asyncio
async def test_with_no_layer_open_recall_opens_no_index(world, monkeypatch, tmp_path):
    _index(world)
    opened = []
    monkeypatch.setattr(context_mod, "_recall_candidates",
                        lambda *a, **kw: opened.append(a) or [])
    fw = _rules(tmp_path, memory={"layer_audience": {"L6": "private"}})
    agent = _agent(world, fw, _Groups(_group([THIS, OTHER], gid="group-m")))
    with pytest.raises(_PromptBuilt):
        real_build = context_mod.build_llm_messages

        def _build(**kwargs):
            real_build(**kwargs)
            raise _PromptBuilt

        monkeypatch.setattr(agent_mod, "build_llm_messages", _build)
        await agent.process(QUERY, "group-m", message_source="group")
    with pytest.raises(_PromptBuilt):
        await agent.process(QUERY, "telegram-5", message_source="telegram")
    assert opened == []


# -- memory_search ------------------------------------------------------------


def _ctx(w, fw, cls, layers, source="group"):
    ctx = ToolContext(agent_root=w.root, firewall=fw, message_source=source,
                      turn_class=cls, open_layers=layers)
    ctx._agent = _agent(w, fw)
    return ctx


def test_memory_search_gives_a_mixed_group_only_l6_rows(world, monkeypatch, tmp_path):
    _index(world)
    real_backend = retrieval_mod.make_backend_for_agent
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=8))
    fw = _rules(tmp_path, memory={"layer_audience": {"L6": "shared"}})
    mixed = memory_search(_ctx(world, fw, OTHER_NODE_TURN, frozenset({"L6"})), QUERY)
    owner = memory_search(_ctx(world, fw, OWNER_TURN, ALL_MEMORY_LAYERS, "ui"), QUERY)
    assert "L6/shared-note.md" in mixed and "knowledge/own-note.md" not in mixed, mixed
    assert "L6/shared-note.md" in owner and "knowledge/own-note.md" in owner, owner


def test_memory_search_refuses_a_group_with_nothing_shared(world, monkeypatch, tmp_path):
    def _no_index(*a, **kw):
        raise AssertionError("an index was opened")

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _no_index)
    answer = memory_search(_ctx(world, _rules(tmp_path), OTHER_NODE_TURN, frozenset()), QUERY)
    assert answer.startswith("memory_search answers only the owner"), answer
    assert "other_node" in answer and "no memory layer is open" in answer, answer


def test_memory_search_still_refuses_a_stranger(world, monkeypatch, tmp_path):
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("opened")))
    answer = memory_search(_ctx(world, _rules(tmp_path), STRANGER_TURN, frozenset(), "telegram"),
                           QUERY)
    assert answer.startswith("memory_search answers only the owner"), answer


# -- prompt sections ----------------------------------------------------------


def _brief(w):
    path = w.tmp / "conversations" / PROFILE / "morning_brief.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": "BRIEF-MARK-55", "consumed": False}), encoding="utf-8")
    return path


def _system(w, layers):
    (w.root / "knowledge" / "_index.md").write_text("# Index\nINDEX-MARK-9", encoding="utf-8")
    messages, _cap = build_llm_messages(
        agent_root=w.root, memory=Memory(w.root), task={"id": "group-m", "text": "hi"},
        recall_results=[], open_layers=layers,
        sandbox_read_only=["/secret/project-tree"])
    return messages[0]["content"][1]["text"], messages[-1]["content"]


@pytest.mark.parametrize("layers", [frozenset({"L6"}), frozenset()])
def test_with_l5_closed_no_memory_section_and_the_brief_is_not_consumed(world, layers):
    brief = _brief(world)
    system, tail = _system(world, layers)
    for marker in ("## Scratchpad", "## Identity", "## Morning Brief", "## Knowledge base",
                   SCRATCH_MARK, "BRIEF-MARK-55", "INDEX-MARK-9"):
        assert marker not in system, marker
    assert json.loads(brief.read_text(encoding="utf-8"))["consumed"] is False
    assert "/secret/project-tree" not in tail


def test_the_owner_s_prompt_keeps_every_section_and_consumes_the_brief(world):
    brief = _brief(world)
    system, tail = _system(world, ALL_MEMORY_LAYERS)
    for marker in ("## Scratchpad", "## Identity", SCRATCH_MARK, "BRIEF-MARK-55", "INDEX-MARK-9"):
        assert marker in system, marker
    assert json.loads(brief.read_text(encoding="utf-8"))["consumed"] is True
    assert "/secret/project-tree" in tail


# -- replayed tails -----------------------------------------------------------

TAIL_L5 = ("\n\n<turn_context>\nActive Recall\n--- ACTIVE RECALL ---\nIf a recalled file ...\n"
           "[L5] knowledge/own-note.md: Own Note — call read_file(\"knowledge/own-note.md\")\n"
           "    OWN-TAIL-MARK\n--- END RECALL ---\n\n\n## Runtime context\n\n{}\n\n"
           "## Extended access\n  - `/secret/project-tree` (read-only)\n</turn_context>")
TAIL_L6 = ("\n\n<turn_context>\nActive Recall\n--- RECALL HINTS: [L6] L6/shared-note.md ---\n"
           "\n\n## Runtime context\n\n{}\n</turn_context>")


def test_a_replayed_tail_naming_a_closed_layer_loses_its_recall_block():
    out = strip_closed_layers(TAIL_L5, frozenset({"L6"}))
    assert "OWN-TAIL-MARK" not in out and "knowledge/own-note.md" not in out
    assert "## Runtime context" in out
    assert "/secret/project-tree" not in out and "## Extended access" in out
    assert strip_closed_layers(TAIL_L6, frozenset({"L6"})) == TAIL_L6


def test_the_builder_strips_replayed_tails_only_on_a_turn_missing_a_layer(world):
    history = [{"id": "m1", "role": "user", "content": "earlier", "msg_index": 1},
               {"id": "m2", "role": "assistant", "content": "answer", "msg_index": 2}]

    def _replayed(layers):
        messages, _ = build_llm_messages(
            agent_root=world.root, memory=Memory(world.root), task={"id": "group-m", "text": "q"},
            conversation_history=history, sent_annotations={"m1": TAIL_L5},
            recall_results=[], open_layers=layers)
        return next(m["content"] for m in messages if m["role"] == "user" and "earlier" in m["content"])

    assert "OWN-TAIL-MARK" not in _replayed(frozenset({"L6"}))
    assert "OWN-TAIL-MARK" in _replayed(ALL_MEMORY_LAYERS)


# -- personal context ---------------------------------------------------------


def _personal_ctx(w, fw, cls, layers, source):
    ctx = _ctx(w, fw, cls, layers, source)
    ctx.dpc_service = SimpleNamespace(firewall=fw)
    return ctx


@pytest.mark.parametrize("cls,layers,source", [
    (STRANGER_TURN, frozenset(), "telegram"),
    (OTHER_NODE_TURN, frozenset({"L5", "L6", "EXT"}), "group"),
])
def test_personal_context_is_never_read_on_a_turn_that_is_not_the_owner_s(
        world, tmp_path, cls, layers, source):
    (world.home / "personal.json").write_text(json.dumps({"profile": {"name": "PERSONAL-MARK"}}),
                                              encoding="utf-8")
    answer = get_dpc_context(_personal_ctx(world, _rules(tmp_path), cls, layers, source), "personal")
    assert "PERSONAL-MARK" not in answer
    assert answer.startswith("⚠️ Personal context is not read on this turn"), answer


def test_the_owner_still_reads_personal_context(world, tmp_path):
    (world.home / "personal.json").write_text(json.dumps({"profile": {"name": "PERSONAL-MARK"}}),
                                              encoding="utf-8")
    answer = get_dpc_context(_personal_ctx(world, _rules(tmp_path), OWNER_TURN, ALL_MEMORY_LAYERS,
                                           "ui"), "personal")
    assert "PERSONAL-MARK" in answer
