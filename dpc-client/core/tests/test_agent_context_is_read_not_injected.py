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

from dpc_client_core.dpc_agent.context import build_llm_messages
from dpc_client_core.dpc_agent.loop import TOOL_RESULT_CHAR_CAP
from dpc_client_core.dpc_agent.memory import Memory
from dpc_client_core.dpc_agent.tools.core import get_dpc_context
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
    return h


def _firewall(tmp_path, personal: bool, device: bool) -> ContextFirewall:
    rules = tmp_path / "privacy_rules.json"
    rules.write_text(json.dumps({"dpc_agent": {
        "enabled": True,
        "personal_context_access": personal,
        "device_context_access": device,
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


def test_the_prompt_builder_ignores_a_context_it_is_handed(tmp_path):
    """The other door: a queued task used to forward task.data['dpc_context']."""
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
# The tool: what it returns
# ---------------------------------------------------------------------------

def _ctx(firewall):
    return types.SimpleNamespace(
        dpc_service=types.SimpleNamespace(firewall=firewall),
        _agent=types.SimpleNamespace(_firewall_profile="agent_001"),
    )


def _json_part(out: str) -> str:
    body = out.split("\n\n", 1)[1]
    return body.split("\n\n- ", 1)[0]


def test_personal_keeps_the_profile_as_json_and_names_the_big_sections(home, tmp_path):
    out = get_dpc_context(_ctx(_firewall(tmp_path, True, True)), "personal")
    path = home / ".dpc" / "personal.json"

    assert len(out) < TOOL_RESULT_CHAR_CAP
    kept = json.loads(_json_part(out))
    assert kept["profile"]["description"] == PERSONAL_MARK
    assert kept["metadata"] == {"version": 3}
    assert "knowledge" not in kept and "commit_history" not in kept
    assert f"knowledge: {N_TOPICS} entries" in out
    assert f"commit_history: {N_COMMITS} entries" in out
    assert str(path) in out
    assert str(home / ".dpc" / "knowledge") in out


def test_personal_too_big_even_without_the_big_sections_is_clipped_with_a_count(
        home, tmp_path):
    (home / ".dpc" / "personal.json").write_text(
        json.dumps(_personal(profile_extra="p" * 40000 + "END-OF-PROFILE")),
        encoding="utf-8")
    out = get_dpc_context(_ctx(_firewall(tmp_path, True, True)), "personal")

    assert len(out) < TOOL_RESULT_CHAR_CAP
    assert "MIDDLE OMITTED" in out
    assert out.count(str(home / ".dpc" / "personal.json")) >= 2  # header + clip note
    assert f"commit_history: {N_COMMITS} entries" in out


def test_device_comes_back_whole(home, tmp_path):
    out = get_dpc_context(_ctx(_firewall(tmp_path, False, True)), "device")
    whole = json.loads((home / ".dpc" / "device_context.json").read_text(encoding="utf-8"))
    assert json.loads(_json_part(out)) == whole


def test_each_switch_still_guards_its_own_type(home, tmp_path):
    only_device = _ctx(_firewall(tmp_path, False, True))
    assert get_dpc_context(only_device, "personal") == \
        "⚠️ Personal context access is disabled via firewall rules"
    assert DEVICE_MARK in get_dpc_context(only_device, "device")

    only_personal = _ctx(_firewall(tmp_path, True, False))
    assert get_dpc_context(only_personal, "device") == \
        "⚠️ Device context access is disabled via firewall rules"
    assert PERSONAL_MARK in get_dpc_context(only_personal, "personal")
