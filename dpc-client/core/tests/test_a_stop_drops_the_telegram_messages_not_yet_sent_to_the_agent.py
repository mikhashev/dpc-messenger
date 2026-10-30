"""Stop, /newsession and /stop drop what a Telegram chat gathered for the agent.

Until 2026-10-30 a burst still waiting for its chat to go quiet reached the
agent after a desktop Stop, after /newsession, and there was no /stop on the
bot at all (Mike's call, 2026-10-30: all three drop it, and say so in one line).

The manager here is a real `DpcAgentManager` without its constructor, so the
Stop goes through `interrupt` as the service calls it; the agent is a fake
that takes the run gate and registers an interrupt event the way a loop does.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.dpc_agent import run_gate
from dpc_client_core.dpc_agent.run_gate import RunGate, hold
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

CHAT_A = "111"
CHAT_B = "222"
AGENT_ID = "agent_stop_burst_test"
CONV_A = f"telegram-{CHAT_A}"
QUIET = 0.3


@pytest.fixture(autouse=True)
def _own_gate(monkeypatch):
    monkeypatch.setitem(run_gate._RUN_GATES, AGENT_ID, RunGate(agent_id=AGENT_ID))


class FakeAgent:
    """Holds the gate like process_message, and runs until Stop sets its event."""

    def __init__(self, gate, manager):
        self.gate = gate
        self.manager = manager
        self.runs = []
        self.started = asyncio.Event()
        self.until_stopped = False
        self.stopped_closes = []

    async def process_message(self, **kw):
        entry = {"conversation_id": kw["conversation_id"], "conversation_title": ""}
        async with hold(self.gate, entry, stoppable=True):
            self.runs.append(kw)
            self.started.set()
            if self.until_stopped:
                ev = asyncio.Event()
                self.manager._interrupt_events[kw["conversation_id"]] = ev
                try:
                    await ev.wait()
                finally:
                    self.manager._interrupt_events.pop(kw["conversation_id"], None)
                return "⚠️ Stopped by user."
            return f"reply {len(self.runs)}"

    def close_stopped(self, **kw):
        self.stopped_closes.append(kw)
        return "⚠️ Stopped by user while waiting for its turn."


def _setup(quiet=QUIET, transcribe=None):
    bridge = AgentTelegramBridge(
        bot_token="t", allowed_chat_ids=[CHAT_A, CHAT_B], burst_quiet_seconds=quiet)
    bridge.footer_enabled = False
    bridge.send_markdown = AsyncMock()
    gate = run_gate._RUN_GATES[AGENT_ID]
    manager = am.DpcAgentManager.__new__(am.DpcAgentManager)
    manager._interrupt_events = {}
    manager._kill_events = {}
    manager._telegram_bridge = bridge
    manager._run_gate = gate
    manager.service = SimpleNamespace(_conversation_display_name=lambda c: "")
    if transcribe is not None:
        manager.service.transcribe_audio = transcribe
    agent = FakeAgent(gate, manager)
    manager._close_turn_stopped_while_waiting = agent.close_stopped
    bridge.set_message_handler(agent.process_message, manager)
    bridge._bot = SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(
        download_to_drive=AsyncMock())))
    return bridge, agent, manager


_next_id = iter(range(1, 10_000))


def _update(chat=CHAT_A, text=None, voice=False):
    u = MagicMock()
    u.effective_chat.id = chat
    u.effective_user.first_name = "Mike"
    u.message.message_id = next(_next_id)
    u.message.text = text
    u.message.reply_text = AsyncMock()
    if voice:
        u.message.voice = SimpleNamespace(duration=2, file_size=10, file_id="voice-file")
    return u


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


def _send(bridge, update):
    if isinstance(update.message.voice, SimpleNamespace):
        return asyncio.create_task(bridge._handle_voice_message(update, _ctx()))
    return asyncio.create_task(bridge._handle_message(update, _ctx()))


async def _finish(tasks, timeout=5.0):
    """Wait for handler tasks; one a drop cancelled ends cancelled, not failed."""
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout)
    for t in tasks:
        if not t.cancelled() and t.exception() is not None:
            raise t.exception()


def _replies(update):
    return [c.args[0] for c in update.message.reply_text.await_args_list]


async def _busy_desktop_run(agent, manager):
    """The agent runs in its desktop chat until a Stop: the loop Stop reaches."""
    agent.until_stopped = True
    run = asyncio.create_task(agent.process_message(conversation_id=AGENT_ID, message="x"))
    await asyncio.wait_for(agent.started.wait(), 2)
    return run


# --- the desktop Stop ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_desktop_stop_in_the_quiet_window_drops_the_gathered_messages_and_says_so_once():
    bridge, agent, manager = _setup()
    run = await _busy_desktop_run(agent, manager)
    agent.runs.clear()
    tasks = [_send(bridge, _update(text="one")), _send(bridge, _update(text="two"))]
    await asyncio.sleep(0.05)  # inside the quiet window, not yet at the gate

    assert manager.interrupt(AGENT_ID) is True
    await _finish(tasks + [run])
    await asyncio.sleep(QUIET * 2)

    assert agent.runs == []
    assert bridge._bursts == {}
    bridge.send_markdown.assert_awaited_once_with(
        CHAT_A, "⏹ Stopped from the desktop; dropped 2 messages not yet sent to the agent.")

    # A message after the drop is a burst of its own and runs normally.
    agent.until_stopped = False
    later = _update(text="later")
    await _finish([_send(bridge, later)])
    assert [r["message"] for r in agent.runs] == ["later"]
    assert _replies(later) == ["reply 1"]


@pytest.mark.asyncio
async def test_a_desktop_stop_drops_a_burst_waiting_at_the_gate_behind_the_run_it_stops():
    bridge, agent, manager = _setup(quiet=0.02)
    run = await _busy_desktop_run(agent, manager)
    agent.runs.clear()
    tasks = [_send(bridge, _update(text="queued"))]
    await asyncio.sleep(0.15)  # past the pause: the burst waits at the gate
    assert [w["conversation_id"] for w in agent.gate.waiters] == [CONV_A]

    assert manager.interrupt(AGENT_ID) is True
    await _finish(tasks + [run])
    await asyncio.sleep(0.1)

    assert agent.runs == []
    assert agent.gate.waiters == []
    bridge.send_markdown.assert_awaited_once_with(
        CHAT_A, "⏹ Stopped from the desktop; dropped 1 message not yet sent to the agent.")


@pytest.mark.asyncio
async def test_a_desktop_stop_with_nothing_gathered_sends_nothing():
    bridge, agent, manager = _setup()
    run = await _busy_desktop_run(agent, manager)
    assert manager.interrupt(AGENT_ID) is True
    await _finish([run])
    await asyncio.sleep(0.05)
    bridge.send_markdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stop_that_already_took_the_burst_out_of_line_keeps_its_own_close():
    """The service's order with no loop running: stop_waiting, then the drop."""
    bridge, agent, manager = _setup(quiet=0.02)
    release = asyncio.Event()

    async def other_chat_run():
        async with agent.gate.hold({"conversation_id": "agent_other", "conversation_title": ""}):
            await release.wait()

    busy = asyncio.create_task(other_chat_run())
    await asyncio.sleep(0)
    last = _update(text="waiting")
    tasks = [_send(bridge, last)]
    await asyncio.sleep(0.15)

    assert run_gate.stop_waiting(CONV_A) == [AGENT_ID]
    assert manager.drop_telegram_bursts() == 0
    await _finish(tasks)
    release.set()
    await _finish([busy])

    assert agent.runs == []
    assert len(agent.stopped_closes) == 1
    assert "Stopped by user" in _replies(last)[0]
    bridge.send_markdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_burst_dropped_while_its_voice_is_transcribed_is_not_run_by_its_owner():
    transcribing = asyncio.Event()
    finish = asyncio.Event()

    async def transcribe(audio_base64, mime_type):
        transcribing.set()
        await finish.wait()
        return {"text": "spoken", "provider": "fake"}

    bridge, agent, manager = _setup(quiet=0.02, transcribe=transcribe)
    release = asyncio.Event()

    async def other_chat_run():
        async with agent.gate.hold({"conversation_id": "agent_other", "conversation_title": ""}):
            await release.wait()

    busy = asyncio.create_task(other_chat_run())
    voice = _send(bridge, _update(voice=True))
    await asyncio.wait_for(transcribing.wait(), 2)

    assert bridge.drop_bursts() == 1
    after = _send(bridge, _update(text="after the drop"))
    await asyncio.sleep(0.01)
    finish.set()
    await asyncio.sleep(0.15)
    # One worker for the new burst, not the old owner beside it.
    assert [w["conversation_id"] for w in agent.gate.waiters] == [CONV_A]
    release.set()
    await _finish([voice, after, busy])

    assert [r["message"] for r in agent.runs] == ["after the drop"]


# --- /newsession --------------------------------------------------------------

@pytest.mark.asyncio
async def test_newsession_drops_the_chats_gathered_messages_and_says_so():
    bridge, agent, manager = _setup()
    manager.service.propose_new_session = AsyncMock(return_value={"status": "success"})
    other = _send(bridge, _update(chat=CHAT_B, text="other chat"))
    tasks = [_send(bridge, _update(text="old one")), _send(bridge, _update(text="old two"))]
    await asyncio.sleep(0.05)

    cmd = _update(text="/newsession")
    await bridge._handle_newsession_command(cmd, _ctx())
    (reply,) = _replies(cmd)
    assert reply.endswith("Start fresh\\!\nDropped 2 messages not yet sent to the agent\\.")
    manager.service.propose_new_session.assert_awaited_once_with(CONV_A)

    await _finish(tasks + [other])
    # The other chat's burst is its own and still runs.
    assert [r["message"] for r in agent.runs] == ["other chat"]


@pytest.mark.asyncio
async def test_newsession_with_nothing_gathered_says_nothing_about_dropping():
    bridge, agent, manager = _setup()
    manager.service.propose_new_session = AsyncMock(return_value={"status": "success"})
    cmd = _update(text="/newsession")
    await bridge._handle_newsession_command(cmd, _ctx())
    (reply,) = _replies(cmd)
    assert "Dropped" not in reply and reply.endswith("Start fresh\\!")


# --- /stop --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_stops_the_running_turn_of_the_chat_and_drops_what_came_after():
    bridge, agent, manager = _setup(quiet=0.02)
    agent.until_stopped = True
    first = _send(bridge, _update(text="long job"))
    await asyncio.wait_for(agent.started.wait(), 2)
    second = _send(bridge, _update(text="meanwhile"))
    await asyncio.sleep(0.01)

    cmd = _update(text="/stop")
    await bridge._handle_stop_command(cmd, _ctx())
    assert _replies(cmd) == [
        "⏹ Stopped the agent's running turn; dropped 1 message not yet sent to the agent."]

    await _finish([first, second])
    await asyncio.sleep(0.1)
    assert [r["message"] for r in agent.runs] == ["long job"]
    assert bridge._bursts == {}
    bridge.send_markdown.assert_not_awaited()  # the reply is the one line


@pytest.mark.asyncio
async def test_stop_with_nothing_running_says_so():
    bridge, agent, manager = _setup()
    cmd = _update(text="/stop")
    await bridge._handle_stop_command(cmd, _ctx())
    assert _replies(cmd) == ["⏹ Nothing was running; no messages were waiting to be sent."]


@pytest.mark.asyncio
async def test_stop_in_the_quiet_window_drops_the_burst_and_a_later_message_runs():
    bridge, agent, manager = _setup()
    tasks = [_send(bridge, _update(text="one")), _send(bridge, _update(text="two"))]
    await asyncio.sleep(0.05)

    cmd = _update(text="/stop")
    await bridge._handle_stop_command(cmd, _ctx())
    assert _replies(cmd) == [
        "⏹ Nothing was running; dropped 2 messages not yet sent to the agent."]
    await _finish(tasks)
    await asyncio.sleep(QUIET * 2)
    assert agent.runs == []

    await _finish([_send(bridge, _update(text="later"))])
    assert [r["message"] for r in agent.runs] == ["later"]


@pytest.mark.asyncio
async def test_stop_takes_the_chats_burst_out_of_the_gates_line_without_counting_it_as_a_turn():
    bridge, agent, manager = _setup(quiet=0.02)
    release = asyncio.Event()

    async def other_chat_run():
        async with agent.gate.hold({"conversation_id": "agent_other", "conversation_title": ""}):
            await release.wait()

    busy = asyncio.create_task(other_chat_run())
    await asyncio.sleep(0)
    tasks = [_send(bridge, _update(text="queued"))]
    await asyncio.sleep(0.15)

    cmd = _update(text="/stop")
    await bridge._handle_stop_command(cmd, _ctx())
    assert _replies(cmd) == [
        "⏹ Nothing was running; dropped 1 message not yet sent to the agent."]
    assert agent.gate.waiters == []
    await _finish(tasks)
    release.set()
    await _finish([busy])
    assert agent.runs == [] and agent.stopped_closes == []


@pytest.mark.asyncio
async def test_stop_is_a_bot_command_listed_beside_newsession():
    bridge, _, _ = _setup()
    app = MagicMock()
    bridge._register_handlers(app)
    commands = [
        sorted(c.args[0].commands)[0] for c in app.add_handler.call_args_list
        if type(c.args[0]).__name__ == "CommandHandler"
    ]
    assert "stop" in commands

    for handler in (bridge._handle_start_command, bridge._handle_help_command):
        cmd = _update(text="/help")
        await handler(cmd, _ctx())
        (text,) = _replies(cmd)
        assert "/stop" in text and text.index("/stop") < text.index("/newsession")
