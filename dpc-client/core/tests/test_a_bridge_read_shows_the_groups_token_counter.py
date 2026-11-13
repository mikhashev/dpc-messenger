"""A bridge read shows the group's token counter, as the desktop UI does.

The backend adds `token_usage` beside `chat_context` in the get_group_chat_context
reply; the bridge prints one [TOKENS] line right after its header. Figures follow
SessionControls.svelte: non-dialog = agent prompt - dialogue estimate (floored at 0),
available = window - non-dialog.
"""

import types
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.service import CoreService

from tests.test_a_bridge_answers_to_the_tag_it_registered import GROUP, bridge  # noqa: F401
from tests.test_every_agent_woken_in_a_group_gets_the_group_block import GID, _service

USAGE = {
    "tokens_used": 97103, "token_limit": 250000, "history_tokens": 16244,
    "tokens_after_last_response": 97103, "tokens_after_last_response_at": "2026-11-13T10:00:00+00:00",
    "context_agent": "Ark",
    "context_agents": [
        {"name": "Ark", "tokens": 97103, "limit": 250000, "percent": 38.8},
        {"name": "Johnny", "tokens": 22000, "limit": 100000, "percent": 22.0},
    ],
}

EXAMPLE = ("[TOKENS] dialog 16244 of 169141 available (10%) · fullest agent Ark 97103 / 250000 (39%)"
           " · agents: Ark 39%, Johnny 22% · non-dialog ≈80859")


def test_the_line_matches_the_counter_the_ui_shows(bridge):  # noqa: F811
    assert bridge.format_token_usage(USAGE) == EXAMPLE


def test_with_no_agent_answered_it_says_so(bridge):  # noqa: F811
    usage = {"token_limit": 16384, "history_tokens": 800, "tokens_after_last_response": 0,
             "tokens_used": 800}
    assert bridge.format_token_usage(usage) == (
        "[TOKENS] dialog 800 of 16384 available (5%) · "
        "no agent has answered since the backend started")


def test_a_zero_window_is_said_not_divided_by(bridge):  # noqa: F811
    usage = dict(USAGE, token_limit=0)
    line = bridge.format_token_usage(usage)
    assert "window not known" in line and "ZeroDivision" not in line
    assert bridge.format_token_usage({"token_limit": 0, "history_tokens": 5}) == (
        "[TOKENS] dialog 5 (window not known) · no agent has answered since the backend started")


def test_a_dialogue_estimate_above_the_measured_prompt_is_clamped(bridge):  # noqa: F811
    usage = dict(USAGE, history_tokens=120000)
    assert bridge.format_token_usage(usage).startswith(
        "[TOKENS] dialog 97103 of 250000 available (39%)")


def test_no_token_usage_is_unknown_with_the_reason(bridge):  # noqa: F811
    assert bridge.format_token_usage(None, "backend not reached: refused") == (
        "[TOKENS] unknown (backend not reached: refused)")
    assert bridge.format_token_usage(None).startswith("[TOKENS] unknown (")


def test_the_read_prints_the_line_after_the_header_and_fetches_once(bridge, monkeypatch, capsys):  # noqa: F811
    calls = []

    def fetch(ids):
        calls.append(list(ids))
        return {GROUP: {"chat_context": {"chat_name": "1234"}, "token_usage": USAGE}}

    monkeypatch.setattr(bridge, "_fetch_chat_replies", fetch)
    bridge.main(["--group", GROUP, "--chat-context"])
    out = capsys.readouterr().out.splitlines()
    assert calls == [[GROUP]]
    assert out[0] == '[CHAT_CONTEXT] {"chat_name": "1234"}'
    assert out[1].startswith("[CC Group Bridge] 0 messages")
    assert out[3] == EXAMPLE


def test_an_unreachable_backend_still_reads_the_history(bridge, monkeypatch, capsys):  # noqa: F811
    def fetch(ids):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(bridge, "_fetch_chat_replies", fetch)
    bridge.main(["--group", GROUP])
    out = capsys.readouterr().out
    assert out.startswith("[CC Group Bridge] 0 messages (last 10)\n\n")
    assert "[TOKENS] unknown (backend not reached: refused)" in out


# the backend side


@pytest.mark.asyncio
async def test_the_reply_carries_token_usage_beside_an_unchanged_chat_context():
    monitor = types.SimpleNamespace(
        persist_history=True, live_history_boundary=None,
        get_message_history=lambda: [{"content": "x" * 400}],
        get_token_usage=lambda: {"token_limit": 16384})
    svc, _ = _service(monitor=monitor)
    svc._group_agent_context = {GID: {"agent_001": (5000, 20000, "2026-11-13T10:00:00+00:00", "Ark")}}
    reply = await svc.get_group_chat_context(GID)
    assert reply["chat_context"] == svc._group_chat_context(GID)
    usage = reply["token_usage"]
    assert "conversation_id" not in usage
    assert usage["history_tokens"] == 100 and usage["tokens_after_last_response"] == 5000
    assert usage["context_agent"] == "Ark" and usage["token_limit"] == 20000


@pytest.mark.asyncio
async def test_a_failure_building_the_counter_leaves_the_reply_whole():
    svc, _ = _service()          # its monitor has no history to read
    reply = await svc.get_group_chat_context(GID)
    assert reply == {"status": "success", "chat_context": svc._group_chat_context(GID)}
