"""Tests for P1 Consciousness Tool Access."""

import pathlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dpc_client_core.dpc_agent.consciousness import (
    BackgroundConsciousness,
    CONSCIOUSNESS_TOOL_WHITELIST,
    _MAX_TOOL_ROUNDS,
)
from dpc_client_core.dpc_agent.tools.core import set_next_wakeup
from dpc_client_core.dpc_agent.tools.registry import ToolContext, CORE_TOOL_NAMES


@pytest.fixture
def mock_agent(tmp_path):
    agent = MagicMock()
    agent.agent_root = tmp_path / "agent"
    agent.agent_root.mkdir(parents=True)
    (agent.agent_root / "logs").mkdir()
    agent.memory = MagicMock()
    agent.memory.load_identity.return_value = "I am a test agent"
    agent.memory.load_scratchpad.return_value = "## Current focus\nTesting"
    agent.memory.list_knowledge_topics.return_value = ["testing", "python"]
    agent.memory.read_jsonl_tail.return_value = []
    agent.tools = MagicMock()
    agent.tools._ctx = MagicMock()
    agent.llm = AsyncMock()
    agent._firewall = None
    agent._user_active = False
    agent.skill_store = MagicMock()
    return agent


def test_whitelist_has_expected_tools():
    assert "update_scratchpad" in CONSCIOUSNESS_TOOL_WHITELIST
    assert "read_file" in CONSCIOUSNESS_TOOL_WHITELIST
    assert "write_file" in CONSCIOUSNESS_TOOL_WHITELIST
    assert "knowledge_list" in CONSCIOUSNESS_TOOL_WHITELIST
    assert "set_next_wakeup" in CONSCIOUSNESS_TOOL_WHITELIST
    assert "run_shell" not in CONSCIOUSNESS_TOOL_WHITELIST
    assert "browse_page" not in CONSCIOUSNESS_TOOL_WHITELIST


def test_whitelist_size():
    assert len(CONSCIOUSNESS_TOOL_WHITELIST) == 5


def test_set_next_wakeup_in_core_tool_names():
    assert "set_next_wakeup" in CORE_TOOL_NAMES


def test_max_tool_rounds():
    assert _MAX_TOOL_ROUNDS == 5


def test_set_next_wakeup_clamping():
    ctx = ToolContext(agent_root=pathlib.Path("/tmp/test"))
    result = set_next_wakeup(ctx, seconds=10)
    assert "60s" in result
    assert len(ctx.pending_events) == 1
    assert ctx.pending_events[0]["seconds"] == 60

    ctx2 = ToolContext(agent_root=pathlib.Path("/tmp/test"))
    result = set_next_wakeup(ctx2, seconds=9999)
    assert "3600s" in result
    assert ctx2.pending_events[0]["seconds"] == 3600


def test_set_next_wakeup_normal():
    ctx = ToolContext(agent_root=pathlib.Path("/tmp/test"))
    result = set_next_wakeup(ctx, seconds=120)
    assert "120s" in result
    assert ctx.pending_events[0]["type"] == "consciousness_set_wakeup"
    assert ctx.pending_events[0]["seconds"] == 120


def test_build_context(mock_agent):
    bc = BackgroundConsciousness(agent=mock_agent)
    ctx = bc._build_context()
    assert "background consciousness mode" in ctx
    assert "I am a test agent" in ctx
    assert "Testing" in ctx
    assert "testing, python" in ctx


def test_setup_consciousness_context(mock_agent):
    bc = BackgroundConsciousness(agent=mock_agent)
    prev = bc._setup_consciousness_context()
    assert prev == mock_agent.tools._ctx
    mock_agent.tools.set_context.assert_called_once()
    new_ctx = mock_agent.tools.set_context.call_args[0][0]
    assert new_ctx.current_task_id == "consciousness"
    assert new_ctx.current_task_type == "consciousness"


def test_get_tool_schemas_filters(mock_agent):
    mock_agent.tools.schemas.return_value = [
        {"function": {"name": "update_scratchpad", "description": "test"}},
        {"function": {"name": "run_shell", "description": "dangerous"}},
        {"function": {"name": "set_next_wakeup", "description": "wakeup"}},
    ]
    bc = BackgroundConsciousness(agent=mock_agent)
    schemas = bc._get_tool_schemas()
    names = [s["function"]["name"] for s in schemas]
    assert "update_scratchpad" in names
    assert "set_next_wakeup" in names
    assert "run_shell" not in names


def test_apply_pending_wakeup(mock_agent):
    bc = BackgroundConsciousness(agent=mock_agent)
    mock_ctx = MagicMock()
    mock_ctx.pending_events = [
        {"type": "consciousness_set_wakeup", "seconds": 180},
        {"type": "other_event", "data": "keep"},
    ]
    mock_agent.tools._ctx = mock_ctx
    bc._apply_pending_wakeup()
    assert bc._next_wakeup_sec == 180
    assert len(mock_ctx.pending_events) == 1
    assert mock_ctx.pending_events[0]["type"] == "other_event"


@pytest.mark.asyncio
async def test_reflect_with_tools_no_tools(mock_agent):
    """When all tools blocked, falls back to pure reflection."""
    mock_agent.tools.schemas.return_value = []
    mock_agent.llm.chat.return_value = (
        {"content": "I reflected on my state.", "tool_calls": []},
        {"prompt_tokens": 100, "completion_tokens": 50},
    )
    bc = BackgroundConsciousness(agent=mock_agent)
    result = await bc._reflect_with_tools()
    assert result == "I reflected on my state."


@pytest.mark.asyncio
async def test_reflect_with_tools_uses_tool(mock_agent):
    """LLM calls a tool, gets result, then produces final content."""
    mock_agent.tools.schemas.return_value = [
        {"function": {"name": "update_scratchpad", "description": "test"}},
    ]
    mock_agent.tools.execute.return_value = "Scratchpad updated."

    call_count = [0]

    async def mock_chat(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return (
                {
                    "content": "",
                    "tool_calls": [{
                        "id": "tc_1",
                        "function": {
                            "name": "update_scratchpad",
                            "arguments": '{"content": "note", "mode": "append"}',
                        },
                    }],
                },
                {"prompt_tokens": 100, "completion_tokens": 50},
            )
        else:
            return (
                {"content": "Done consolidating.", "tool_calls": []},
                {"prompt_tokens": 150, "completion_tokens": 30},
            )

    mock_agent.llm.chat = mock_chat
    bc = BackgroundConsciousness(agent=mock_agent)
    result = await bc._reflect_with_tools()
    assert result == "Done consolidating."
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_user_active_breaks_loop(mock_agent):
    """Tool loop exits when user becomes active."""
    mock_agent.tools.schemas.return_value = [
        {"function": {"name": "set_next_wakeup", "description": "test"}},
    ]
    mock_agent._user_active = True
    mock_agent.llm.chat = AsyncMock()

    bc = BackgroundConsciousness(agent=mock_agent)
    result = await bc._reflect_with_tools()
    assert result == ""
    mock_agent.llm.chat.assert_not_called()
