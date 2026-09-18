"""Personal and device context reach an agent only through get_dpc_context.

2026-09-18 (Mike's call): the per-agent switches mean "may read through the
tool", never "paste into the prompt". The pasted block went into the per-turn
tail, and tails are replayed byte-for-byte for prefix caching, so every trigger
added the whole block for good — one agent reached 775k tokens in four days.
"""

import json
import pathlib
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.context import build_llm_messages
from dpc_client_core.dpc_agent.loop import TOOL_RESULT_CHAR_CAP
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.task_queue import Task
from dpc_client_core.dpc_agent.tools.core import get_dpc_context
from dpc_client_core.dpc_agent.tools.registry import ToolContext
from dpc_client_core.firewall import ContextFirewall
from dpc_client_core.managers import agent_manager as am

PERSONAL_MARK = "PERSONAL-MARK-7f3a"
DEVICE_MARK = "DEVICE-MARK-9c1e"
FORBIDDEN = ("## DPC Context", "<PERSONAL_CONTEXT>", "<DEVICE_CONTEXT>",
             PERSONAL_MARK, DEVICE_MARK)
N_TOPICS = 400
N_COMMITS = 350


def _personal(profile_extra: str = "") -> dict:
    return {
        "profile": {"name": "Tester", "description": PERSONAL_MARK + profile_extra},
        "preferences": {"language": "en"},
        "knowledge": {
            f"topic {i}": {"summary": "k" * 600,
                           "markdown_file": f"knowledge/topic_{i}.md"}
            for i in range(N_TOPICS)
        },
        "commit_history": [
            {"commit_id": f"c{i}", "message": "m" * 800} for i in range(N_COMMITS)
        ],
        "metadata": {"version": 3},
    }


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".dpc").mkdir(parents=True)
    (h / ".dpc" / "personal.json").write_text(
        json.dumps(_personal(), indent=2), encoding="utf-8")
    (h / ".dpc" / "device_context.json").write_text(
        json.dumps({"hardware": {"gpu": {"model": DEVICE_MARK}},
                    "software": {"os": {"family": "Windows"}}}, indent=2),
        encoding="utf-8")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: h))
    monkeypatch.delenv("DPC_HOME", raising=False)
    return h


def _firewall(tmp_path, personal: bool, device: bool, *,
              knowledge: bool = False, read_only=()) -> ContextFirewall:
    """Defaults leave ~/.dpc out of reach of read_file: no sandbox_extensions
    (the shipped default) and no shared-knowledge access."""
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {
        "enabled": True,
        "personal_context_access": personal,
        "device_context_access": device,
        "human_knowledge_access": knowledge,
        "sandbox_extensions": {"read_only": [str(x) for x in read_only]},
    }}))
    return ContextFirewall(rules)


# ---------------------------------------------------------------------------
# The manager: a chat run hands the agent nothing, even with every switch on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_chat_run_hands_the_agent_no_context_with_both_switches_on(
        home, tmp_path):
    agent_root = tmp_path / "agent_001"
    Memory(agent_root).ensure_files()
    seen = {}

    class FakeAgent:
        _last_usage = None
        _last_trace = None
        _last_cap_info = None

        async def process(self, **kwargs):
            seen.update(kwargs)
            # Build the real prompt from what the manager handed over, so the
            # check covers the tail the model would have received.
            messages, cap = build_llm_messages(
                agent_root=agent_root, memory=Memory(agent_root),
                task={"id": kwargs["conversation_id"], "type": "chat",
                      "text": kwargs["message"]},
                dpc_context=kwargs.get("dpc_context"),
            )
            seen["prompt"] = json.dumps(messages, ensure_ascii=False)
            seen["tail"] = cap.get("turn_context") or ""
            return "ok"

    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager.agent_id = "agent_001"
    manager.firewall = _firewall(tmp_path, personal=True, device=True)
    manager.config = {}
    manager._agent = FakeAgent()
    manager._agents = {}
    manager._memory_indexes_initialized = True
    manager._agent_display_name = "Ark"
    manager._interrupt_events = {}
    manager._daily_tokens_used = 0
    manager._daily_tokens_date = ""
    manager.service = types.SimpleNamespace(
        p2p_manager=types.SimpleNamespace(node_id="dpc-node-me"),
        local_api=types.SimpleNamespace(broadcast_event=AsyncMock()),
    )
    monitor = MagicMock()
    monitor.message_history = []
    manager.ensure_started = AsyncMock(return_value=manager)
    manager._get_or_create_agent_monitor = lambda conv: monitor
    manager.get_session_state = lambda conv: {}
    manager._resolve_reasoning_effort = lambda conv, effort: (None, "none")
    manager._resolve_context_window = lambda: None

    result = await manager._process_message_guarded(
        "hello", "agent_001", include_context=True)

    assert result == "ok"
    assert not seen.get("dpc_context")
    for needle in FORBIDDEN:
        assert needle not in seen["prompt"], needle
        assert needle not in seen["tail"], needle


def test_the_prompt_builder_ignores_a_dpc_context_kwarg(tmp_path):
    """The kwarg is kept for old callers; handing it over pastes nothing."""
    agent_root = tmp_path / "agent_001"
    Memory(agent_root).ensure_files()
    messages, cap = build_llm_messages(
        agent_root=agent_root, memory=Memory(agent_root),
        task={"id": "agent_001", "type": "chat", "text": "hi"},
        dpc_context={"personal": PERSONAL_MARK, "device": DEVICE_MARK},
    )
    text = json.dumps(messages, ensure_ascii=False) + (cap.get("turn_context") or "")
    for needle in FORBIDDEN:
        assert needle not in text, needle


# ---------------------------------------------------------------------------
# The other door: a queued task used to forward task.data['dpc_context']
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("task_type", ["chat", "check_back"])
async def test_a_queued_task_does_not_forward_its_dpc_context(task_type, tmp_path):
    agent = DpcAgent.__new__(DpcAgent)
    agent._task_handlers = {}
    agent.agent_root = tmp_path / "agent_001"
    agent.process = AsyncMock(return_value="ok")
    task = Task(id="t1", task_type=task_type, data={
        "text": "wake up",
        "dpc_context": {"personal": PERSONAL_MARK, "device": DEVICE_MARK},
    })

    assert await agent._execute_task_guarded(task) == "ok"

    agent.process.assert_awaited_once()
    args, kwargs = agent.process.call_args
    assert "dpc_context" not in kwargs
    assert PERSONAL_MARK not in json.dumps([args, kwargs], default=str)


# ---------------------------------------------------------------------------
# The tool: what it returns
# ---------------------------------------------------------------------------

def _ctx(firewall, tmp_path):
    ctx = ToolContext(agent_root=tmp_path / "agent_001",
                      dpc_service=types.SimpleNamespace(firewall=firewall),
                      firewall=firewall)
    ctx._agent = types.SimpleNamespace(_firewall_profile="agent_001")
    return ctx


def _json_part(out: str) -> str:
    body = out.split("\n\n", 1)[1]
    return body.split("\n\n- ", 1)[0]


def _notes(out: str) -> str:
    return out.split("\n\n- ", 1)[1]


NOT_READABLE = "not readable from this agent's sandbox"


def test_personal_keeps_the_profile_as_json_and_counts_the_big_sections(home, tmp_path):
    out = get_dpc_context(_ctx(_firewall(tmp_path, True, True), tmp_path), "personal")

    assert len(out) < TOOL_RESULT_CHAR_CAP
    kept = json.loads(_json_part(out))
    assert kept["profile"]["description"] == PERSONAL_MARK
    assert kept["metadata"] == {"version": 3}
    assert "knowledge" not in kept and "commit_history" not in kept
    assert f"knowledge: {N_TOPICS} entries" in out
    assert f"commit_history: {N_COMMITS} entries" in out


def test_a_path_the_sandbox_refuses_is_not_offered(home, tmp_path):
    """The default: ~/.dpc is outside the sandbox and no extension grants it.
    Ark measured read_file on the old pointer: "Sandbox violation"."""
    out = get_dpc_context(_ctx(_firewall(tmp_path, True, True), tmp_path), "personal")
    notes = _notes(out)

    assert str(home / ".dpc") not in notes
    assert str(home / ".dpc") not in out
    assert "read_file" not in notes
    assert notes.count(NOT_READABLE) == 2  # commit_history and knowledge
    assert "Agent Permissions" in notes


def test_a_path_the_sandbox_admits_is_offered(home, tmp_path):
    fw = _firewall(tmp_path, True, True, knowledge=True, read_only=[home / ".dpc"])
    out = get_dpc_context(_ctx(fw, tmp_path), "personal")
    notes = _notes(out)

    assert NOT_READABLE not in notes
    assert f'Read the "commit_history" field of {home / ".dpc" / "personal.json"}' in notes
    assert f"top-level .md file in {home / '.dpc' / 'knowledge'}" in notes


def test_knowledge_alone_is_offered_when_only_its_gate_is_open(home, tmp_path):
    """The two gates are separate: shared knowledge without sandbox_extensions."""
    fw = _firewall(tmp_path, True, True, knowledge=True)
    notes = _notes(get_dpc_context(_ctx(fw, tmp_path), "personal"))

    assert f"top-level .md file in {home / '.dpc' / 'knowledge'}" in notes
    assert "commit_history: " in notes and f"personal.json is {NOT_READABLE}" in notes


def test_personal_too_big_even_without_the_big_sections_is_clipped_with_a_count(
        home, tmp_path):
    (home / ".dpc" / "personal.json").write_text(
        json.dumps(_personal(profile_extra="p" * 40000 + "END-OF-PROFILE")),
        encoding="utf-8")
    out = get_dpc_context(_ctx(_firewall(tmp_path, True, True), tmp_path), "personal")

    assert len(out) < TOOL_RESULT_CHAR_CAP
    assert "MIDDLE OMITTED" in out
    assert "clipped and does not parse" in _notes(out)
    assert f"commit_history: {N_COMMITS} entries" in out


def test_device_comes_back_whole_when_under_the_budget(home, tmp_path):
    out = get_dpc_context(_ctx(_firewall(tmp_path, False, True), tmp_path), "device")
    whole = json.loads((home / ".dpc" / "device_context.json").read_text(encoding="utf-8"))
    assert json.loads(_json_part(out)) == whole
    assert "\n\n- " not in out  # no notes: nothing clipped


@pytest.mark.parametrize("readable", [False, True])
def test_device_over_the_budget_is_clipped_and_says_so(home, tmp_path, readable):
    path = home / ".dpc" / "device_context.json"
    path.write_text(json.dumps({
        "hardware": {"gpu": {"model": DEVICE_MARK}},
        "ai_models": [{"name": f"model-{i}", "size": "x" * 200} for i in range(200)],
    }, indent=2), encoding="utf-8")
    fw = _firewall(tmp_path, False, True,
                   read_only=[home / ".dpc"] if readable else ())
    out = get_dpc_context(_ctx(fw, tmp_path), "device")

    assert len(out) < TOOL_RESULT_CHAR_CAP
    assert "MIDDLE OMITTED" in out
    notes = _notes(out)
    assert "clipped and does not parse" in notes
    if readable:
        assert str(path) in notes and NOT_READABLE not in notes
    else:
        assert str(path) not in out and NOT_READABLE in notes


def test_dpc_home_is_honoured_like_the_knowledge_gate(home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "device_context.json").write_text(
        json.dumps({"hardware": {"gpu": {"model": "ELSEWHERE-GPU"}}}), encoding="utf-8")
    monkeypatch.setenv("DPC_HOME", str(elsewhere))

    out = get_dpc_context(_ctx(_firewall(tmp_path, False, True), tmp_path), "device")

    assert "ELSEWHERE-GPU" in out and DEVICE_MARK not in out


def test_each_switch_still_guards_its_own_type(home, tmp_path):
    only_device = _ctx(_firewall(tmp_path, False, True), tmp_path)
    assert get_dpc_context(only_device, "personal") == \
        "⚠️ Personal context access is disabled via firewall rules"
    assert DEVICE_MARK in get_dpc_context(only_device, "device")

    only_personal = _ctx(_firewall(tmp_path, True, False), tmp_path)
    assert get_dpc_context(only_personal, "device") == \
        "⚠️ Device context access is disabled via firewall rules"
    assert PERSONAL_MARK in get_dpc_context(only_personal, "personal")
