"""Recall and memory_search serve the owner's turns only; every door says whose turn it is.

Active Recall and memory_search could not tell a Discord or Telegram stranger's turn
from the owner's own, so the owner's memory reached both alike
(RECALL-AND-MEMORY-SEARCH-SERVE-EVERY-TURN-ALIKE-WHATEVER-ITS-SOURCE). Mike's call,
2026-11-07: every entry point names its source in `message_source`, one predicate
(`is_owner_caller`) decides, an unnamed source is not the owner, a Discord turn reads
nothing outside the sandbox, and a scheduled task keeps the source of the turn that
scheduled it.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import context as context_mod
from dpc_client_core.dpc_agent import retrieval as retrieval_mod
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.retrieval.base import TextAddItem
from dpc_client_core.dpc_agent.task_queue import Task
from dpc_client_core.dpc_agent.tools.core import memory_search, read_file, schedule_task
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.message_handlers.group_handler import GroupTextHandler

from tests.test_every_agent_woken_in_a_group_gets_the_group_block import GID, PEER
from tests.test_every_agent_woken_in_a_group_gets_the_group_block import _service as _group_service
from tests.test_a_peer_agents_mention_wakes_one_agent_and_the_reply_wakes_nobody_across_nodes import (  # noqa: F401
    _agent_post, _receiver, author,
)

PROFILE = "agent_x"
QUERY = "harbour inventory ledger"
OWN_MARK = "OWN-BODY-MARK-5b1e"
EXT_MARK = "EXT-BODY-MARK-90c4"
PACKAGE = pathlib.Path(agent_mod.__file__).resolve().parents[1]

# A group is no longer an owner's door: its members decide (ADR-048, and
# test_a_turn_sees_only_the_memory_layers_its_audience_may.py).
OWNER = ["ui", "external_agent"]
NOT_OWNER = ["telegram", "discord", None, "telegram_public", "somewhere-else"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dpc").mkdir(parents=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("DPC_HOME", str(home / ".dpc"))

    agent_root = tmp_path / "agents" / PROFILE
    (agent_root / "knowledge").mkdir(parents=True)
    Memory(agent_root).ensure_files()
    own = agent_root / "knowledge" / "own-note.md"
    own.write_text(f"# Own Note\n{QUERY} {OWN_MARK}", encoding="utf-8")

    ext_root = tmp_path / "projects" / "open-project"
    ext_root.mkdir(parents=True)
    ext = ext_root / "notes.md"
    ext.write_text(f"# Open Notes\n{QUERY} {EXT_MARK}", encoding="utf-8")

    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({
        "dpc_agent": {"enabled": True},
        "agent_profiles": {PROFILE: {
            "memory": {"enabled": True, "active_recall": True},
            "sandbox_extensions": {"read_only": [str(ext_root)], "extended_read_enabled": True},
        }},
    }), encoding="utf-8")
    return {"agent_root": agent_root, "own": own, "ext": ext,
            "firewall": ContextFirewall(rules)}


def _agent(w):
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = w["agent_root"]
    agent.memory = Memory(w["agent_root"])
    agent.skill_store = agent._embedding_provider = None
    agent._firewall = w["firewall"]
    agent._firewall_profile = PROFILE
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    return agent


def _ctx(w, source):
    ctx = ToolContext(agent_root=w["agent_root"], firewall=w["firewall"], message_source=source)
    ctx._agent = _agent(w)
    return ctx


class _PromptBuilt(Exception):
    pass


async def _turn_tail(w, monkeypatch, source) -> str:
    """process() up to the prompt, through the real recall and the real builder."""
    real_build = context_mod.build_llm_messages
    real_backend = retrieval_mod.make_backend_for_agent
    seen = {}

    def _build(**kwargs):
        _messages, cap = real_build(**kwargs)
        seen["tail"] = cap.get("turn_context") or ""
        raise _PromptBuilt

    monkeypatch.setattr(agent_mod, "build_llm_messages", _build)
    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent",
                        lambda root, **kw: real_backend(root, dimensions=8))
    monkeypatch.setattr(context_mod, "_get_knowledge_graph", lambda root: None)
    with pytest.raises(_PromptBuilt):
        await _agent(w).process(QUERY, "conv-1", message_source=source)
    return seen["tail"]


def _index(w):
    text = w["own"].read_text(encoding="utf-8")
    backend = retrieval_mod.make_backend_for_agent(w["agent_root"], dimensions=8)
    backend.text.add([TextAddItem(text=text, meta={
        "source_file": "knowledge/own-note.md", "source_layer": "L5",
        "source_path": str(w["own"]), "heading": "Own Note", "text": text})])
    backend.text.save()


# -- the predicate -----------------------------------------------------------


@pytest.mark.parametrize("source", OWNER)
def test_the_owner_s_doors_are_the_owner(source):
    assert agent_mod.is_owner_caller(source) is True


@pytest.mark.parametrize("source", NOT_OWNER)
def test_telegram_discord_and_an_unnamed_source_are_not(source):
    assert agent_mod.is_owner_caller(source) is False


# -- recall ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("source", OWNER)
async def test_an_owner_turn_gets_its_hints_as_before(world, monkeypatch, source):
    _index(world)
    assert OWN_MARK in await _turn_tail(world, monkeypatch, source)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", NOT_OWNER)
async def test_a_turn_from_anyone_else_gets_no_hint(world, monkeypatch, source):
    _index(world)
    tail = await _turn_tail(world, monkeypatch, source)
    assert OWN_MARK not in tail
    assert "Own Note" not in tail


# -- memory_search -----------------------------------------------------------


@pytest.mark.parametrize("source", NOT_OWNER)
def test_memory_search_refuses_anyone_else_before_opening_an_index(world, monkeypatch, source):
    def _no_index(*a, **kw):
        raise AssertionError("an index was opened")

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _no_index)
    answer = memory_search(_ctx(world, source), QUERY)
    assert answer.startswith("memory_search answers only the owner; this turn came from "), answer
    assert OWN_MARK not in answer


@pytest.mark.parametrize("source", OWNER)
def test_memory_search_still_searches_for_the_owner(world, monkeypatch, source):
    opened = []

    def _index_opened(*a, **kw):
        opened.append(True)
        raise RuntimeError("stop here")

    monkeypatch.setattr(retrieval_mod, "make_backend_for_agent", _index_opened)
    answer = memory_search(_ctx(world, source), QUERY)
    assert opened, answer
    assert "answers only the owner" not in answer


# -- read_file outside the sandbox -------------------------------------------


def test_a_discord_turn_reads_no_extended_path(world):
    answer = read_file(_ctx(world, "discord"), str(world["ext"]))
    assert answer.startswith("⚠️ Access denied"), answer
    assert "discord" in answer
    assert EXT_MARK not in answer


def test_a_discord_turn_no_longer_reads_the_agent_s_memory(world):
    """ADR-048: a stranger opens nothing in the agent directory but its turn's tool results."""
    answer = read_file(_ctx(world, "discord"), "knowledge/own-note.md")
    assert OWN_MARK not in answer and "not the owner's" in answer


def test_a_ui_turn_reads_an_extended_path_as_before(world):
    assert EXT_MARK in read_file(_ctx(world, "ui"), str(world["ext"]))


def test_a_telegram_turn_that_is_not_the_owner_s_reads_no_extended_path(world):
    """ADR-048: EXT is closed to strangers whatever its mark."""
    answer = read_file(_ctx(world, "telegram"), str(world["ext"]))
    assert EXT_MARK not in answer and "not the owner's" in answer


def test_a_group_turn_is_no_longer_an_owner_s_door(world, monkeypatch):
    assert agent_mod.is_owner_caller("group") is False


# -- scheduled tasks keep the source -----------------------------------------


def _schedule_ctx(source):
    scheduled = []

    def _schedule(task_type, data, priority, delay_seconds):
        task = SimpleNamespace(id=f"task-{len(scheduled)}", task_type=task_type, data=data)
        scheduled.append(task)
        return task

    agent = SimpleNamespace(queue=SimpleNamespace(_queue=[]), schedule_task=_schedule,
                            _task_handlers={})
    return SimpleNamespace(_agent=agent, current_task_id="agent_001", check_back_depth=0,
                           reply_telegram_chat_id=None, message_source=source,
                           _scheduled=scheduled)


@pytest.mark.parametrize("source", ["ui", "telegram"])
def test_schedule_task_stores_the_turn_s_source(source):
    ctx = _schedule_ctx(source)
    assert schedule_task(ctx, "chat", '{"text": "later"}').startswith("✓")
    assert ctx._scheduled[0].data["_message_source"] == source


def test_the_model_cannot_name_a_source_of_its_own():
    ctx = _schedule_ctx("telegram")
    schedule_task(ctx, "chat", '{"text": "later", "_message_source": "ui"}')
    assert ctx._scheduled[0].data["_message_source"] == "telegram"


def _executor_agent():
    agent = DpcAgent.__new__(DpcAgent)
    agent._task_handlers = {}
    agent._telegram_send_fn = None
    calls = []

    async def _process(*args, **kwargs):
        calls.append(kwargs)
        return "done"

    agent.process = _process
    return agent, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["chat", "check_back", "review"])
@pytest.mark.parametrize("source", ["ui", "telegram"])
async def test_a_queued_task_runs_with_the_source_it_was_scheduled_from(task_type, source):
    agent, calls = _executor_agent()
    task = Task(id="t1", task_type=task_type, data={
        "text": "look again", "_reply_conversation_id": "agent_001",
        "_message_source": source})
    await agent._execute_task_guarded(task)
    assert calls[-1].get("message_source") == source


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["chat", "check_back", "review"])
async def test_a_queued_task_with_no_source_runs_as_nobody(task_type):
    agent, calls = _executor_agent()
    task = Task(id="t1", task_type=task_type, data={
        "text": "look again", "_reply_conversation_id": "agent_001"})
    await agent._execute_task_guarded(task)
    assert "message_source" in calls[-1]
    assert calls[-1]["message_source"] is None


@pytest.mark.asyncio
async def test_a_custom_task_type_runs_with_its_stored_source():
    from dpc_client_core.dpc_agent.task_types import TaskTypeDefinition

    agent, calls = _executor_agent()
    definition = TaskTypeDefinition(task_type="weather_report", description="d",
                                    execution_prompt="Report on {location}")
    handler = agent._create_task_type_handler(definition)
    await handler({"location": "Oslo", "_message_source": "ui"})
    await handler({"location": "Oslo"})
    assert [c.get("message_source", "absent") for c in calls] == ["ui", None]


@pytest.mark.asyncio
async def test_a_task_from_the_task_board_is_the_owner_s():
    from dpc_client_core.agent_service import AgentService

    captured = {}

    def _schedule(task_type, data, priority, delay_seconds):
        captured.update(data)
        return SimpleNamespace(id="t1", scheduled_at=None)

    async def _started():
        return None

    manager = SimpleNamespace(ensure_started=_started,
                              _agent=SimpleNamespace(schedule_task=_schedule))
    service = AgentService.__new__(AgentService)
    service.llm_manager = SimpleNamespace(
        providers={"dpc_agent": SimpleNamespace(_managers={"agent_001": manager})})
    result = await service.schedule_agent_task(
        agent_id="agent_001", task_type="chat", data={"text": "x", "_message_source": "discord"})
    assert result["status"] == "success", result
    assert captured["_message_source"] == "ui"


# -- every door names its source ---------------------------------------------


@pytest.mark.asyncio
async def test_a_local_group_mention_is_a_group_turn():
    svc, manager = _group_service()
    await svc._invoke_agent_in_group(GID, "@Ark hi", "Mike", "agent_001")
    assert manager.process_message.await_args.kwargs.get("message_source") == "group"


@pytest.mark.asyncio
async def test_a_peer_s_group_mention_is_a_group_turn_with_the_peer_s_node_id():
    svc, manager = _group_service()
    await GroupTextHandler(svc)._invoke_agent(GID, "@Ark hi", "Win10 box", sender_node_id=PEER)
    kwargs = manager.process_message.await_args.kwargs
    assert kwargs.get("message_source") == "group"
    assert kwargs.get("sender_node_id") == PEER


@pytest.mark.asyncio
async def test_the_peer_path_hands_the_author_s_node_id_to_the_wake(author):  # noqa: F811
    _, _, payload = await _agent_post(author, "@Ark can you check the build?")
    _, handler = _receiver(author)
    await handler.handle(author[0], payload)
    assert handler._invoke_agent.await_args.kwargs.get("sender_node_id") == author[0]


@pytest.mark.asyncio
async def test_a_cc_mention_is_an_external_agent_turn():
    svc, _ = _group_service()
    manager = SimpleNamespace(process_message=AsyncMock(return_value=""))
    await svc._invoke_agent_in_agent_chat("agent_001", "@Ark look", manager)
    assert manager.process_message.await_args.kwargs.get("message_source") == "external_agent"


def test_the_agent_takes_the_sender_s_node_id():
    assert "sender_node_id" in inspect.signature(DpcAgent.process).parameters


def _calls_in(path: pathlib.Path, names=("process_message", "_message_handler", "process")):
    """Every call to an agent entry point in a file: (enclosing function, call name, keywords)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in names:
                kws = {k.arg: k.value for k in node.keywords if k.arg}
                found.append((fn.name, node.func.attr, kws))
    return found


def _source_of(kws):
    value = kws.get("message_source")
    if value is None:
        return None
    return _value_of(value)


# A source a door names by its constant rather than by a literal.
_NAMED_SOURCES = {"TELEGRAM_OWNER_MESSAGE_SOURCE": agent_mod.TELEGRAM_OWNER_MESSAGE_SOURCE}


def _value_of(node):
    """A literal, a known constant, or `a if cond else b` read as the set of both."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id in _NAMED_SOURCES:
        return _NAMED_SOURCES[node.id]
    if isinstance(node, ast.IfExp):
        return frozenset({_value_of(node.body), _value_of(node.orelse)})
    return "<expr>"


EXPECTED = {
    ("service.py", "_execute_agent_query"): "ui",
    ("service.py", "send_image"): "ui",
    # The source of the chat that woke CC, read from the history (external_agent when
    # only the owner's records are there): test_a_turn_from_telegram_keeps_its_source_and_its_chat.py
    ("service.py", "_invoke_agent_in_agent_chat"): "<expr>",
    ("service.py", "_invoke_agent_in_group"): "group",
    ("message_handlers/group_handler.py", "_invoke_agent"): "group",
    # The owner's own messages in his private chat with the bot are his turn
    # (telegram_owner_user_id, Mike's call, 2026-11-08); every other one is "telegram".
    ("managers/agent_telegram_bridge.py", "_run_burst_batch"):
        frozenset({"telegram", agent_mod.TELEGRAM_OWNER_MESSAGE_SOURCE}),
    ("coordinators/telegram_coordinator.py", "handle_text_message"): "telegram",
    ("coordinators/discord_coordinator.py", "handle_mention"): "discord",
    ("knowledge_service.py", "_agent_auto_revise_proposal"): "ui",
    ("providers/dpc_agent_provider.py", "_generate_response_impl"): "ui",
    ("providers/dpc_agent_provider.py", "_generate_response_stream_impl"): "ui",
}


def test_every_call_into_an_agent_names_its_source():
    """A door with no source runs as nobody, so a door that forgets is a regression."""
    missing = []
    total = 0
    for path in PACKAGE.rglob("*.py"):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "managers/agent_manager.py":
            continue  # the manager forwards; its own call is checked below
        for fn, name, kws in _calls_in(path, names=("process_message", "_message_handler")):
            total += 1
            if _source_of(kws) is None:
                missing.append(f"{rel}:{fn}")
    assert total >= len(EXPECTED), "the sweep found fewer doors than are known"
    assert not missing, missing


@pytest.mark.parametrize("where,expected", list(EXPECTED.items()),
                         ids=[f"{f}:{fn}" for f, fn in EXPECTED])
def test_each_door_passes_its_own_value(where, expected):
    rel, fn_name = where
    calls = [kws for fn, _name, kws in _calls_in(PACKAGE / rel,
                                                 names=("process_message", "_message_handler"))
             if fn == fn_name]
    assert calls, f"no call into the agent in {rel}:{fn_name}"
    assert {_source_of(k) for k in calls} == {expected}


def test_the_manager_hands_the_source_and_the_sender_on_to_the_agent():
    calls = [kws for fn, name, kws in _calls_in(PACKAGE / "managers/agent_manager.py",
                                                names=("process",))
             if fn == "_process_message_guarded"]
    assert len(calls) == 1
    for key in ("message_source", "sender_node_id"):
        value = calls[0].get(key)
        assert isinstance(value, ast.Name) and value.id == key, key


def test_every_queued_run_hands_its_stored_source_back():
    """The executor's own process() calls: none may run without saying whose it is."""
    calls = [(fn, kws) for fn, name, kws in _calls_in(PACKAGE / "dpc_agent/agent.py",
                                                      names=("process",))
             if fn in ("_execute_task_guarded", "_execute_review", "handler")]
    assert len(calls) >= 4
    assert all("message_source" in kws for _fn, kws in calls), calls


@pytest.mark.parametrize("rel", ["eval/gaia/run_gaia_eval.py", "eval/loop/run_loop_eval.py",
                                 "eval/loop/seed_history.py"])
def test_the_eval_harnesses_run_as_the_owner(rel):
    """A bench run with no source would lose recall and memory_search without saying so."""
    path = PACKAGE.parents[2] / rel
    if not path.exists():
        pytest.skip(f"{rel} is not in this checkout")
    calls = [kws for _fn, _name, kws in _calls_in(path, names=("process",))]
    assert calls, f"no agent.process call in {rel}"
    assert {_source_of(k) for k in calls} == {"ui"}
