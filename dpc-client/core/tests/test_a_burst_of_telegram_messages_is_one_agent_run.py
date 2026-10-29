"""Messages a person sends in a row on Telegram reach the agent as one run.

Until 2026-10-29 every text, voice or photo update started its own run, and
the runs queued on the agent's run gate one per message. The bridge now holds
a chat's messages until the chat is quiet (BURST_QUIET_SECONDS since the last
one, BURST_CEILING_SECONDS since the first at most); while the agent is busy it
waits at the gate itself and whatever arrived by the moment the run starts goes
into that run.

The fake agent takes the gate the way `DpcAgentManager.process_message` does,
so the pass-through of a gate its own task holds is exercised too.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.dpc_agent import run_gate
from dpc_client_core.dpc_agent.run_gate import RunGate, agent_turn_snapshot, hold
from dpc_client_core.managers import agent_telegram_bridge
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

CHAT_A = "111"
CHAT_B = "222"
AGENT_ID = "agent_burst_test"
QUIET = 0.08
CEILING = 0.4


@pytest.fixture(autouse=True)
def _windows(monkeypatch):
    monkeypatch.setattr(agent_telegram_bridge, "BURST_QUIET_SECONDS", QUIET)
    monkeypatch.setattr(agent_telegram_bridge, "BURST_CEILING_SECONDS", CEILING)


@pytest.fixture(autouse=True)
def _own_gate(monkeypatch):
    """A gate of this test's own, not one left in the registry by another."""
    monkeypatch.setitem(run_gate._RUN_GATES, AGENT_ID, RunGate(agent_id=AGENT_ID))


class FakeAgent:
    """process_message as the manager has it: hold the gate, then run."""

    def __init__(self, gate):
        self.gate = gate
        self.runs = []
        self.block = None  # an Event the run waits on, when set
        self.started = asyncio.Event()
        self.stopped_closes = []

    async def process_message(self, **kw):
        entry = {"conversation_id": kw["conversation_id"], "conversation_title": ""}
        async with hold(self.gate, entry, stoppable=True):
            self.runs.append(kw)
            self.started.set()
            if self.block is not None:
                await self.block.wait()
            return f"reply {len(self.runs)}"

    def close_stopped(self, **kw):
        self.stopped_closes.append(kw)
        return "⚠️ Stopped by user while waiting for its turn."


def _bridge(transcribe=None):
    bridge = AgentTelegramBridge(bot_token="t", allowed_chat_ids=[CHAT_A, CHAT_B])
    bridge.footer_enabled = False
    gate = run_gate._RUN_GATES[AGENT_ID]
    agent = FakeAgent(gate)
    service = SimpleNamespace(_conversation_display_name=lambda c: "")
    if transcribe is not None:
        service.transcribe_audio = transcribe
    manager = SimpleNamespace(
        _run_gate=gate,
        service=service,
        get_session_state=lambda c: None,
        _close_turn_stopped_while_waiting=agent.close_stopped,
    )
    bridge.set_message_handler(agent.process_message, manager)
    bridge._bot = SimpleNamespace(get_file=AsyncMock(side_effect=_fake_file))
    bridge._keep_incoming_photo = lambda conv, data, mid: {"type": "image", "telegram_message_id": mid}
    return bridge, agent


async def _fake_file(file_id):
    return SimpleNamespace(
        download_to_drive=AsyncMock(),
        download_as_bytearray=AsyncMock(return_value=bytearray(file_id.encode())),
    )


_next_id = iter(range(1, 10_000))


def _update(chat=CHAT_A, text=None, voice=False, photo=None, caption=None, name="Mike"):
    u = MagicMock()
    u.effective_chat.id = chat
    u.effective_user.first_name = name
    u.message.message_id = next(_next_id)
    u.message.text = text
    u.message.caption = caption
    u.message.reply_text = AsyncMock()
    if voice:
        u.message.voice = SimpleNamespace(duration=2, file_size=10, file_id="voice-file")
    if photo:
        u.message.photo = [SimpleNamespace(file_id="small"), SimpleNamespace(file_id=photo)]
    return u


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


def _send(bridge, update):
    """One update as Telegram hands it to a block=False handler: its own task."""
    if update.message.photo and not isinstance(update.message.photo, MagicMock):
        coro = bridge._handle_photo_message(update, _ctx())
    elif isinstance(update.message.voice, SimpleNamespace):
        coro = bridge._handle_voice_message(update, _ctx())
    else:
        coro = bridge._handle_message(update, _ctx())
    return asyncio.create_task(coro)


async def _settle(tasks, timeout=5.0):
    await asyncio.wait_for(asyncio.gather(*tasks), timeout)


def _replies(update):
    return [c.args[0] for c in update.message.reply_text.await_args_list]


@pytest.mark.asyncio
async def test_three_texts_within_the_window_are_one_run_in_order():
    bridge, agent = _bridge()
    updates = [_update(text=t) for t in ("first", "second", "third")]
    tasks = []
    for u in updates:
        tasks.append(_send(bridge, u))
        await asyncio.sleep(0.01)
    await _settle(tasks)

    assert len(agent.runs) == 1
    message = agent.runs[0]["message"]
    assert message.startswith("[3 messages sent in a row]")
    assert message.index("first") < message.index("second") < message.index("third")
    assert agent.runs[0]["telegram_chat_id"] == CHAT_A
    # One answer, to the last message of the batch.
    assert _replies(updates[-1]) == ["reply 1"]
    assert _replies(updates[0]) == [] and _replies(updates[1]) == []


@pytest.mark.asyncio
async def test_a_single_message_reaches_the_agent_as_written():
    bridge, agent = _bridge()
    await _settle([_send(bridge, _update(text="just this"))])
    assert [r["message"] for r in agent.runs] == ["just this"]


@pytest.mark.asyncio
async def test_texts_sent_while_the_agent_is_busy_go_into_the_run_that_starts_when_it_frees():
    bridge, agent = _bridge()
    release = asyncio.Event()

    async def other_chat_run():
        async with agent.gate.hold({"conversation_id": "agent_other", "conversation_title": ""}):
            await release.wait()

    busy = asyncio.create_task(other_chat_run())
    await asyncio.sleep(0)
    tasks = [_send(bridge, _update(text="one")), _send(bridge, _update(text="two"))]
    await asyncio.sleep(QUIET * 3)  # well past the quiet window: still nothing runs

    assert agent.runs == []
    waiting = agent_turn_snapshot(AGENT_ID)["waiting"]
    assert [w["conversation_id"] for w in waiting] == [f"telegram-{CHAT_A}"]

    tasks.append(_send(bridge, _update(text="three")))
    await asyncio.sleep(0.02)
    release.set()
    await _settle(tasks + [busy])

    assert len(agent.runs) == 1
    message = agent.runs[0]["message"]
    assert message.startswith("[3 messages sent in a row]")
    assert message.index("one") < message.index("two") < message.index("three")


@pytest.mark.asyncio
async def test_a_message_after_the_run_started_goes_to_the_next_run():
    bridge, agent = _bridge()
    agent.block = asyncio.Event()
    tasks = [_send(bridge, _update(text="before"))]
    await asyncio.wait_for(agent.started.wait(), 2)

    tasks.append(_send(bridge, _update(text="after")))
    await asyncio.sleep(QUIET * 2)
    assert len(agent.runs) == 1
    agent.block.set()
    await _settle(tasks)

    assert [r["message"] for r in agent.runs] == ["before", "after"]


@pytest.mark.asyncio
async def test_the_ceiling_starts_a_run_during_a_stream_that_never_goes_quiet():
    bridge, agent = _bridge()
    tasks = []
    sent_when_first_run = None
    for i in range(30):  # one message every QUIET/3 for ~0.8 s, twice the ceiling
        tasks.append(_send(bridge, _update(text=f"m{i:02d}")))
        await asyncio.sleep(QUIET / 3)
        if agent.runs and sent_when_first_run is None:
            sent_when_first_run = i + 1
    await _settle(tasks)

    assert sent_when_first_run is not None and sent_when_first_run < 30
    delivered = "\n\n".join(r["message"] for r in agent.runs)
    order = [delivered.index(f"m{i:02d}") for i in range(30)]
    assert order == sorted(order)
    assert 2 <= len(agent.runs) < 30


@pytest.mark.asyncio
async def test_a_command_and_a_button_bypass_the_gathering(monkeypatch):
    monkeypatch.setattr(agent_telegram_bridge, "BURST_QUIET_SECONDS", 0.5)
    bridge, agent = _bridge()
    text = _send(bridge, _update(text="gathered"))
    await asyncio.sleep(0.02)

    status = _update(text="/status")
    await bridge._handle_status_command(status, _ctx())
    assert status.message.reply_text.await_count == 1  # answered at once
    assert agent.runs == []                            # while the text still waits

    query = MagicMock()
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message.chat.id = CHAT_A
    query.data = "shell:req-1:approve"
    bridge._agent_manager.service.shell_approve_command = AsyncMock(return_value={"status": "ok"})
    await bridge._handle_shell_callback(SimpleNamespace(callback_query=query), _ctx())
    query.edit_message_text.assert_awaited_once()
    assert agent.runs == []

    await _settle([text])
    assert [r["message"] for r in agent.runs] == ["gathered"]


@pytest.mark.asyncio
async def test_a_stop_in_the_chat_drops_what_was_gathered_behind_a_busy_agent():
    bridge, agent = _bridge()
    release = asyncio.Event()

    async def other_chat_run():
        async with agent.gate.hold({"conversation_id": "agent_other", "conversation_title": ""}):
            await release.wait()

    busy = asyncio.create_task(other_chat_run())
    await asyncio.sleep(0)
    updates = [_update(text="one"), _update(text="two")]
    tasks = [_send(bridge, u) for u in updates]
    await asyncio.sleep(QUIET * 3)

    # The Stop the desktop sends for a chat with no running loop (interrupt_agent).
    assert run_gate.stop_waiting(f"telegram-{CHAT_A}") == [AGENT_ID]
    await _settle(tasks)
    release.set()
    await _settle([busy])
    await asyncio.sleep(QUIET * 2)

    assert agent.runs == []
    assert bridge._bursts == {}
    (closed,) = agent.stopped_closes
    assert closed["conversation_id"] == f"telegram-{CHAT_A}"
    assert "one" in closed["message"] and "two" in closed["message"]
    assert "Stopped by user" in _replies(updates[-1])[0]

    # The chat is not left gathering: the next message runs on its own.
    await _settle([_send(bridge, _update(text="later"))])
    assert [r["message"] for r in agent.runs] == ["later"]


@pytest.mark.asyncio
async def test_a_voice_message_in_the_middle_keeps_its_place_with_its_transcript():
    async def transcribe(audio_base64, mime_type):
        await asyncio.sleep(QUIET * 2)  # slower than the quiet window
        return {"text": "spoken words", "provider": "fake"}

    bridge, agent = _bridge(transcribe=transcribe)
    voice = _update(voice=True)
    tasks = [_send(bridge, _update(text="typed before"))]
    await asyncio.sleep(0.01)
    tasks.append(_send(bridge, voice))
    await asyncio.sleep(0.01)
    tasks.append(_send(bridge, _update(text="typed after")))
    await _settle(tasks)

    assert len(agent.runs) == 1
    message = agent.runs[0]["message"]
    assert message.index("typed before") < message.index("spoken words") < message.index("typed after")
    # The transcript is still shown to the sender, as before.
    assert any("Transcription" in r for r in _replies(voice))


@pytest.mark.asyncio
async def test_a_voice_message_with_no_speech_leaves_the_rest_of_the_burst_alone():
    bridge, agent = _bridge(transcribe=AsyncMock(return_value={"text": ""}))
    tasks = [_send(bridge, _update(text="a")), _send(bridge, _update(voice=True)),
             _send(bridge, _update(text="b"))]
    await _settle(tasks)
    assert len(agent.runs) == 1
    assert agent.runs[0]["message"] == "[2 messages sent in a row]\n\na\n\nb"


@pytest.mark.asyncio
async def test_a_photo_is_its_own_run_after_the_text_before_it_and_order_holds():
    bridge, agent = _bridge()
    photo = _update(photo="big-file", caption="look")
    tasks = [_send(bridge, _update(text="t1")), _send(bridge, _update(text="t2"))]
    await asyncio.sleep(0.01)
    tasks.append(_send(bridge, photo))
    await asyncio.sleep(0.01)
    tasks.append(_send(bridge, _update(text="t3")))
    await _settle(tasks)

    assert len(agent.runs) == 3
    assert "t1" in agent.runs[0]["message"] and "t2" in agent.runs[0]["message"]
    assert agent.runs[0].get("image_base64") is None
    assert agent.runs[1]["message"] == "look"
    assert agent.runs[1]["image_base64"]
    assert agent.runs[1]["attachments"] == [{"type": "image", "telegram_message_id": photo.message.message_id}]
    assert agent.runs[2]["message"] == "t3"
    assert _replies(photo) == ["reply 2"]


@pytest.mark.asyncio
async def test_two_chats_do_not_mix():
    bridge, agent = _bridge()
    tasks = [
        _send(bridge, _update(chat=CHAT_A, text="a1")),
        _send(bridge, _update(chat=CHAT_B, text="b1")),
        _send(bridge, _update(chat=CHAT_A, text="a2")),
    ]
    await _settle(tasks)

    by_chat = {r["telegram_chat_id"]: r for r in agent.runs}
    assert len(agent.runs) == 2
    assert by_chat[CHAT_A]["conversation_id"] == f"telegram-{CHAT_A}"
    assert "a1" in by_chat[CHAT_A]["message"] and "a2" in by_chat[CHAT_A]["message"]
    assert "b" not in by_chat[CHAT_A]["message"].replace("[2 messages sent in a row]", "")
    assert by_chat[CHAT_B]["message"] == "b1"


@pytest.mark.asyncio
async def test_a_task_holding_the_gate_passes_through_it_and_another_task_still_waits():
    gate = RunGate(agent_id="agent_reentry")
    inner_done = asyncio.Event()
    other_got_it = []

    async def other():
        async with gate.hold({"conversation_id": "x", "conversation_title": ""}):
            other_got_it.append(inner_done.is_set())

    async with gate.hold({"conversation_id": "c", "conversation_title": ""}):
        spawned = asyncio.create_task(other())
        async with gate.hold({"conversation_id": "c", "conversation_title": ""}):
            await asyncio.sleep(0.01)
            assert gate.turn["conversation_id"] == "c"
        inner_done.set()
        assert gate.locked()  # the inner exit did not release the outer hold
    await asyncio.wait_for(spawned, 2)
    assert other_got_it == [True]
    assert not gate.locked()
