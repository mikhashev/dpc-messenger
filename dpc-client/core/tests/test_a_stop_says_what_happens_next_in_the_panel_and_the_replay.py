"""A Stop says what happens next, to the person and to the agent (Mike, 2026-11-22).

Before this a first press showed nothing until the loop next looked at it (minutes, on
a local model mid-call), the summary request the loop sends was shown nowhere, and the
record of the stopped turn did not say the user had pressed Stop. Now:

* the press is put in the progress panel the moment it lands, on the ordinary
  `agent_progress` event with `stop_note` set ("Stop pressed by <who>", a Kill too);
* the loop says it when it starts the summary call (tools off) and when a Stop lands
  before the first step;
* the replay of a stopped or killed turn opens with one line that the user pressed Stop,
  a function of the record alone (the same history renders the same bytes), and sleep
  still reads past the record.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.context import (
    USER_KILLED_MARK,
    USER_STOPPED_MARK,
    history_body,
)
from dpc_client_core.dpc_agent.loop import STOP_NOTE_KINDS, run_llm_loop, stop_note_text
from dpc_client_core.dpc_agent.sleep_pipeline import _format_archive_messages
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.service import CoreService
from tests.test_a_stop_ends_with_a_summary_and_a_kill_reaches_the_running_tool import (
    SUMMARY,
    _asks,
    _call,
    _Llm,
    _says,
    _Tools,
)

CID = "agent_001"
DASH = "—"


@pytest.fixture(autouse=True)
def _no_agent_config(monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})


# --- the texts ------------------------------------------------------------------------


def test_the_lines_say_who_pressed_and_default_to_the_user():
    assert stop_note_text("pressed", "Mike") == \
        f"Stop pressed by Mike {DASH} waiting for the current step to finish"
    assert stop_note_text("pressed") == \
        f"Stop pressed by the user {DASH} waiting for the current step to finish"
    assert stop_note_text("kill", "Mike (Telegram)") == \
        f"Kill pressed by Mike (Telegram) {DASH} ending now, no summary"
    assert stop_note_text("summary") == \
        f"Writing a summary of where it stopped {DASH} tools are off (the user pressed Stop)"
    assert "nothing was done" in stop_note_text("before_round_1")
    assert all(stop_note_text(k) for k in STOP_NOTE_KINDS)
    with pytest.raises(ValueError):
        stop_note_text("nonsense")


# --- the loop's lines -------------------------------------------------------------------


class _Order:
    """What the panel was told and what the model was asked, in the order it happened."""

    def __init__(self, stop):
        self.events: list = []
        self.stop = stop

    def emit(self, message, tool=None, round=None, tool_calls=None, speed=None, stop_note=None):
        if stop_note:
            self.events.append(("note", stop_note, message, round))
            return
        self.events.append(("progress", message))
        if isinstance(message, str) and message.startswith("✓"):
            self.stop.set()


class _OrderedLlm(_Llm):
    def __init__(self, order, *steps):
        super().__init__(*steps)
        self.order = order

    async def chat(self, messages, **kwargs):
        self.order.events.append(("call", kwargs.get("tool_choice")))
        return await super().chat(messages, **kwargs)


@pytest.mark.asyncio
async def test_the_summary_call_is_announced_before_it_is_made(tmp_path):
    stop, kill = asyncio.Event(), asyncio.Event()
    order = _Order(stop)
    llm = _OrderedLlm(order, _asks(_call("probe", args={"what": "dmesg"})), _says(SUMMARY))

    text, _u, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "why is the camera not seen"}],
        tools=_Tools(), llm=llm, agent_root=tmp_path / CID, emit_progress=order.emit,
        task_id="t1", conversation_id=CID, stop_event=stop, kill_event=kill,
    )

    assert text == SUMMARY and trace["turn_end"] == "stopped"
    notes = [e for e in order.events if e[0] == "note"]
    assert [n[1] for n in notes] == ["summary"], "one line, the summary's"
    assert notes[0][2] == stop_note_text("summary")
    note_at = order.events.index(notes[0])
    calls = [i for i, e in enumerate(order.events) if e[0] == "call"]
    assert calls[-1] > note_at, "said before the summary call goes out"
    assert order.events[calls[-1]] == ("call", "none")


@pytest.mark.asyncio
async def test_a_stop_before_the_first_step_says_nothing_was_done(tmp_path):
    stop = asyncio.Event()
    stop.set()
    order = _Order(stop)
    llm = _OrderedLlm(order)

    text, _u, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "hi"}],
        tools=_Tools(), llm=llm, agent_root=tmp_path / CID, emit_progress=order.emit,
        task_id="t1", conversation_id=CID, stop_event=stop, kill_event=asyncio.Event(),
    )

    assert trace["turn_end"] == "stopped" and llm.calls == []
    assert [e[1] for e in order.events if e[0] == "note"] == ["before_round_1"]


@pytest.mark.asyncio
async def test_an_emitter_that_does_not_know_stop_notes_cannot_break_the_stop(tmp_path):
    stop, kill = asyncio.Event(), asyncio.Event()

    def old_emitter(message, tool=None, round=None, tool_calls=None, speed=None):
        if isinstance(message, str) and message.startswith("✓"):
            stop.set()

    llm = _Llm(_asks(_call("probe", args={"what": "dmesg"})), _says(SUMMARY))
    text, _u, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "go"}],
        tools=_Tools(), llm=llm, agent_root=tmp_path / CID, emit_progress=old_emitter,
        task_id="t1", conversation_id=CID, stop_event=stop, kill_event=kill,
    )
    assert text == SUMMARY and trace["turn_end"] == "stopped"


# --- the press, where it lands ------------------------------------------------------------


class _Wire:
    """local_api.broadcast_event collecting what the UI would receive."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def broadcast_event(self, name, payload):
        self.sent.append((name, payload))


def _manager(wire, running_in=(CID,)):
    m = DpcAgentManager.__new__(DpcAgentManager)
    m.agent_id = CID
    m._interrupt_events = {cid: asyncio.Event() for cid in running_in}
    m._kill_events = {cid: asyncio.Event() for cid in running_in}
    m.service = SimpleNamespace(local_api=wire)
    return m


def _notes(wire):
    return [(p["stop_note"], p["message"], p["conversation_id"], p["tool_name"])
            for name, p in wire.sent if name == "agent_progress" and p.get("stop_note")]


@pytest.mark.asyncio
async def test_the_press_is_in_the_panel_at_once_with_who_pressed_it():
    wire = _Wire()
    m = _manager(wire)

    assert m.interrupt(CID, who="Mike") is True
    await asyncio.sleep(0)  # the broadcast is a task; the loop has not run a step

    assert not m._kill_events[CID].is_set()
    assert _notes(wire) == [(
        "pressed", f"Stop pressed by Mike {DASH} waiting for the current step to finish", CID, None,
    )]


@pytest.mark.asyncio
async def test_the_event_is_the_ordinary_progress_event_with_a_stop_note_added():
    wire = _Wire()
    _manager(wire).interrupt(CID, who="Mike")
    await asyncio.sleep(0)

    name, payload = wire.sent[0]
    assert name == "agent_progress"
    assert payload["stop_note"] == "pressed"
    assert payload["message"].startswith("Stop pressed by Mike")
    assert {"conversation_id", "tool_name", "round", "agent_id", "ts"} <= set(payload)


@pytest.mark.asyncio
async def test_a_kill_is_said_with_who_and_a_repeated_press_says_nothing_new():
    wire = _Wire()
    m = _manager(wire)

    m.interrupt(CID, who="Mike")
    m.interrupt(CID, who="Mike")             # a second plain Stop: nothing new
    m.interrupt(CID, kill=True, who="Mike")  # the Kill
    m.interrupt(CID, kill=True, who="Mike")  # and again: nothing new
    await asyncio.sleep(0)

    assert [n[0] for n in _notes(wire)] == ["pressed", "kill"]
    assert _notes(wire)[1][1] == f"Kill pressed by Mike {DASH} ending now, no summary"
    assert m._kill_events[CID].is_set()


@pytest.mark.asyncio
async def test_with_no_name_the_line_says_the_user():
    wire = _Wire()
    _manager(wire).interrupt(CID)
    await asyncio.sleep(0)
    assert _notes(wire)[0][1].startswith("Stop pressed by the user ")


@pytest.mark.asyncio
async def test_a_stop_where_no_loop_runs_says_nothing():
    wire = _Wire()
    m = _manager(wire, running_in=())
    assert m.interrupt(CID, who="Mike") is False
    await asyncio.sleep(0)
    assert wire.sent == []


def _service_over(m, p2p):
    provider = SimpleNamespace(_managers={CID: m}, _manager=None, get_manager=lambda aid: m)
    svc = CoreService.__new__(CoreService)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    svc.p2p_manager = p2p
    return svc


@pytest.mark.asyncio
async def test_the_desktop_stop_names_the_person_the_node_knows():
    wire = _Wire()
    svc = _service_over(_manager(wire), SimpleNamespace(get_display_name=lambda: "Mike"))

    res = await svc.interrupt_agent(agent_id=CID, conversation_id=CID)
    await asyncio.sleep(0)

    assert res["status"] == "stopped"
    assert _notes(wire)[0][1] == f"Stop pressed by Mike {DASH} waiting for the current step to finish"


@pytest.mark.asyncio
async def test_a_node_that_cannot_name_the_person_still_stops_and_says_the_user():
    wire = _Wire()
    svc = _service_over(_manager(wire), None)

    res = await svc.interrupt_agent(agent_id=CID, conversation_id=CID)
    await asyncio.sleep(0)

    assert res["status"] == "stopped"
    assert _notes(wire)[0][1].startswith("Stop pressed by the user ")


# --- what the agent reads on the turns after -----------------------------------------------


def _rec(idx, role, content, **extra):
    return {"id": f"m{idx}", "role": role, "content": content, "msg_index": idx,
            "timestamp": f"2026-11-22T10:00:0{idx}Z",
            "sender_name": "Ark" if role == "assistant" else "Mike", **extra}


def test_a_stopped_turns_summary_is_replayed_with_the_line_that_the_user_stopped_it():
    rec = _rec(2, "assistant", SUMMARY, turn_end="stopped")

    assert history_body(rec) == f"{USER_STOPPED_MARK}\n{SUMMARY}"
    assert USER_STOPPED_MARK == "[The user pressed Stop and ended this turn early.]"


def test_a_killed_turn_is_replayed_as_a_kill_with_no_summary():
    rec = _rec(2, "assistant", "⚠️ Stopped by user after 3 rounds.", turn_end="killed")

    assert history_body(rec) == f"{USER_KILLED_MARK}\n⚠️ Stopped by user after 3 rounds."
    assert USER_KILLED_MARK == \
        "[The user pressed Stop, then Kill: this turn was ended at once, with no summary.]"


def test_a_stop_text_that_already_says_so_gets_no_second_line():
    text = "⚠️ Stopped by user after 2 rounds."
    assert history_body(_rec(2, "assistant", text, turn_end="stopped")) == text


def test_an_answer_and_the_other_ends_are_replayed_as_before():
    assert history_body(_rec(2, "assistant", "An answer.")) == "An answer."
    assert history_body(_rec(2, "assistant", "No answer: busy.", turn_end="refused")) == "No answer: busy."
    assert history_body(_rec(2, "assistant", "x", turn_end="guard")) == "x"


def test_the_same_record_renders_the_same_bytes_every_time():
    rec = _rec(2, "assistant", SUMMARY, turn_end="stopped")
    assert history_body(rec) == history_body(dict(rec)) == history_body(rec)


def test_sleep_reads_past_a_stopped_turn_and_never_sees_the_line():
    text = _format_archive_messages({"messages": [
        _rec(1, "user", "go"),
        _rec(2, "assistant", SUMMARY, turn_end="stopped"),
        _rec(3, "assistant", "A real answer."),
    ]})
    assert "pressed Stop" not in text and SUMMARY not in text
    assert text.splitlines() == ["[Mike]: go", "[Ark]: A real answer."]
