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

Told everywhere (Mike, 2026-11-22, options 1a + 2 + 3):

* a group's stop summary is posted with a first line that names who stopped it, so the room
  can tell a stop from an answer (text only: the signed format and DPTP are untouched);
* the Stop lines are kept on the finished turn's record, local and unsigned like
  `speed_summary`, and the finished message's progress block draws them;
* the Telegram chat the turn answers is told at the press: Stop, Kill, a stop before the
  first step; a repeated press says nothing, and /stop's own reply is that line.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.context import (
    USER_KILLED_MARK,
    USER_STOPPED_MARK,
    history_body,
)
from dpc_client_core.dpc_agent.loop import (
    STOP_NOTE_KINDS, group_stop_post_text, run_llm_loop, stop_note_text,
)
from dpc_client_core.dpc_agent.sleep_pipeline import _format_archive_messages
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.service import CoreService
from dpc_protocol.message_signing import message_content_hash
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from tests import test_a_stop_drops_the_telegram_messages_not_yet_sent_to_the_agent as tg
from tests.test_a_stop_drops_the_telegram_messages_not_yet_sent_to_the_agent import _own_gate  # noqa: F401  (autouse)
from tests.test_the_finished_strip_is_stored_with_the_message import (  # noqa: F401  (the fixture)
    GROUP,
    TOOL_CALLS,
    _monitor,
    _record_answer,
    _Service,
    author,
)
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


# =============================================================================================
# 1a. A group's stop summary carries a visible first line
# =============================================================================================

PREFIX = f"⚠️ Stopped by Mike {DASH} summary of where it stopped:"


def test_a_stopped_summary_gets_a_first_line_naming_who_stopped_it():
    assert group_stop_post_text(SUMMARY, "stopped", "Mike") == f"{PREFIX}\n\n{SUMMARY}"
    assert group_stop_post_text(SUMMARY, "stopped") == \
        f"⚠️ Stopped by the user {DASH} summary of where it stopped:\n\n{SUMMARY}"
    assert group_stop_post_text(SUMMARY, "stopped", "  ").startswith("⚠️ Stopped by the user ")


def test_a_stop_with_no_summary_names_who_in_the_loops_own_notice():
    assert group_stop_post_text("⚠️ Stopped by user after 3 rounds.", "killed", "Mike") == \
        "⚠️ Stopped by Mike after 3 rounds."
    assert group_stop_post_text(
        "⚠️ Stopped by user after 3 rounds.\n\nNo summary: the model's answer was empty.",
        "stopped", "Mike") == \
        "⚠️ Stopped by Mike after 3 rounds.\n\nNo summary: the model's answer was empty."
    # nobody to name: the loop's text already says "user"
    assert group_stop_post_text("⚠️ Stopped by user after 3 rounds.", "stopped") == \
        "⚠️ Stopped by user after 3 rounds."
    # a killed turn has no summary, so no summary line is made up for its text
    assert group_stop_post_text("something", "killed", "Mike") == "something"


def test_nothing_else_is_touched_and_a_text_is_never_marked_twice():
    for turn_end in (None, "failed", "refused", "guard"):
        assert group_stop_post_text(SUMMARY, turn_end, "Mike") == SUMMARY
    assert group_stop_post_text("", "stopped", "Mike") == ""
    once = group_stop_post_text(SUMMARY, "stopped", "Mike")
    assert group_stop_post_text(once, "stopped", "Mike") == once


def test_the_replay_adds_no_second_mark_to_the_new_first_line():
    text = group_stop_post_text(SUMMARY, "stopped", "Mike")
    assert history_body(_rec(2, "assistant", text, turn_end="stopped")) == text
    assert USER_STOPPED_MARK not in history_body(_rec(2, "assistant", text, turn_end="stopped"))
    # and a summary without it still gets the mark, as before
    assert history_body(_rec(2, "assistant", SUMMARY, turn_end="stopped")).startswith(USER_STOPPED_MARK)


@pytest.mark.asyncio
async def test_a_group_posts_the_stopped_turn_with_the_line_signed_and_verified(author, monkeypatch):
    node_id, signer = author
    service = _Service(node_id, signer, monkeypatch)
    woke = []

    async def mentions(*a, **kw):
        woke.append(a)

    service._handle_group_agent_mentions = mentions
    await CoreService.send_group_agent_message(
        service, GROUP, "Ark", SUMMARY, turn_end="stopped", stopped_by="Mike")

    expected = f"{PREFIX}\n\n{SUMMARY}"
    record = service.monitor.get_message_history()[-1]
    assert record["content"] == expected
    ui = [p for n, p in service.local_api.events if n == "group_text_received"][-1]
    assert ui["text"] == expected
    wire = service.broadcast[-1]["payload"]
    assert wire["text"] == expected
    # The post is an ordinary signed post: a peer verifies it, and there is no new field.
    _, verdict, _ = GroupTextHandler(service=None)._authenticate_author(node_id, wire)
    assert verdict == "verified"
    assert "turn_end" not in wire and "turn_end" not in record
    # Locally it still wakes nobody; what a peer does with a mention in it is the peer's
    # (the post carries no turn_end, and the fix for that needs the signed format).
    assert woke == []


@pytest.mark.asyncio
async def test_a_group_post_of_an_answer_or_a_killed_turn_keeps_its_text(author, monkeypatch):
    node_id, signer = author
    service = _Service(node_id, signer, monkeypatch)
    await CoreService.send_group_agent_message(service, GROUP, "Ark", "A plain answer.", stopped_by="Mike")
    await CoreService.send_group_agent_message(
        service, GROUP, "Ark", "⚠️ Stopped by user after 2 rounds.", turn_end="killed", stopped_by="Mike")
    texts = [m["content"] for m in service.monitor.get_message_history()]
    assert texts == ["A plain answer.", "⚠️ Stopped by Mike after 2 rounds."]


@pytest.mark.asyncio
async def test_the_group_run_hands_the_presser_and_the_lines_to_the_post():
    notes = [{"kind": "pressed", "who": "Mike", "ts": "t", "text": "x"},
             {"kind": "summary", "who": "Mike", "ts": "t", "text": "y"}]
    manager = SimpleNamespace(
        process_message=AsyncMock(return_value=SUMMARY),
        take_stop_notes=lambda cid: list(notes),
        _last_used_agent=SimpleNamespace(_last_trace={"turn_end": "stopped"}),
    )
    provider = SimpleNamespace(_ensure_manager=AsyncMock(return_value=manager))
    svc = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"dpc_agent": provider}),
        _group_chat_context=lambda g: "", _get_agent_display_name=lambda a: "Ark",
        send_group_agent_message=AsyncMock(),
    )
    await CoreService._invoke_agent_in_group(svc, "group-1", "go", "Mike", "agent_001")
    kw = svc.send_group_agent_message.await_args.kwargs
    assert kw["turn_end"] == "stopped" and kw["stopped_by"] == "Mike" and kw["stop_notes"] == notes


# =============================================================================================
# 2. The Stop lines stay with the finished turn
# =============================================================================================

NOTES = [
    {"kind": "pressed", "who": "Mike", "ts": "2026-11-22T10:00:00+00:00",
     "text": f"Stop pressed by Mike {DASH} waiting for the current step to finish"},
    {"kind": "summary", "who": "Mike", "ts": "2026-11-22T10:00:05+00:00",
     "text": f"Writing a summary of where it stopped {DASH} tools are off (the user pressed Stop)"},
]


def test_the_lines_of_a_turn_are_kept_for_its_record_and_taken_once():
    m = _manager(_Wire())
    m._open_stop_notes(CID)
    m.interrupt(CID, who="Mike")
    m._emit_progress(stop_note_text("summary"), CID, stop_note="summary")  # the loop says it, no name
    notes = m.take_stop_notes(CID)

    assert [n["kind"] for n in notes] == ["pressed", "summary"]
    assert [n["who"] for n in notes] == ["Mike", "Mike"], "the loop's line names whose Stop began it"
    assert notes[0]["text"] == f"Stop pressed by Mike {DASH} waiting for the current step to finish"
    assert notes[1]["text"] == stop_note_text("summary")
    assert all(n["ts"] for n in notes) and set(notes[0]) == {"kind", "who", "ts", "text"}
    assert m.take_stop_notes(CID) == []


def test_a_kill_after_a_stop_names_each_presser_and_a_new_turn_starts_empty():
    m = _manager(_Wire())
    m.interrupt(CID, who="Mike")
    m.interrupt(CID, kill=True, who="Anna")
    notes = m.take_stop_notes(CID)
    assert [(n["kind"], n["who"]) for n in notes] == [("pressed", "Mike"), ("kill", "Anna")]

    m.interrupt(CID, who="Mike")                 # lines a turn left behind ...
    m._open_stop_notes(CID)                      # ... are not the next turn's
    assert m.take_stop_notes(CID) == []


def test_a_stop_with_no_loop_keeps_no_lines():
    m = _manager(_Wire(), running_in=())
    assert m.interrupt(CID, who="Mike") is False
    assert m.take_stop_notes(CID) == []


def test_the_record_of_a_stopped_turn_keeps_them_and_reads_back_after_a_reload(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    m = _manager(_Wire())
    m.agent_id, m._agent_display_name = "agent_strip", "Ark"
    m.interrupt(CID, who="Mike")
    m._emit_progress(stop_note_text("summary"), CID, stop_note="summary")
    agent = SimpleNamespace(_last_usage={}, _last_trace={"turn_end": "stopped"})
    DpcAgentManager._record_answer(m, monitor, CID, agent, SUMMARY, [])
    record = monitor.get_message_history()[-1]

    assert record["turn_end"] == "stopped"
    assert [n["kind"] for n in record["stop_notes"]] == ["pressed", "summary"]
    assert record["stop_notes"][0]["text"].startswith("Stop pressed by Mike")

    path = tmp_path / "conversations" / "agent_strip" / "history.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["messages"][-1]["stop_notes"] == record["stop_notes"]
    reloaded = _monitor(tmp_path, monkeypatch)
    assert reloaded.load_history()
    assert reloaded.get_message_history()[-1]["stop_notes"] == record["stop_notes"]


def test_an_answer_and_an_old_record_carry_no_field(tmp_path, monkeypatch):
    record = _record_answer(_monitor(tmp_path, monkeypatch), {"accumulated_tool_calls": TOOL_CALLS})
    assert "stop_notes" not in record
    # a record written before the field existed replays as it did
    old = {"id": "m9", "role": "assistant", "content": SUMMARY, "msg_index": 9, "turn_end": "stopped",
           "timestamp": "2026-11-20T10:00:00Z", "sender_name": "Ark"}
    assert history_body(old) == f"{USER_STOPPED_MARK}\n{SUMMARY}"


def test_the_lines_are_outside_the_signature_the_digest_and_the_chain(tmp_path, monkeypatch):
    def add(monitor, notes):
        monitor.add_message("assistant", "the answer", timestamp="2026-11-22T10:00:00+00:00",
                            sender_node_id="agent_strip", sender_name="Ark", message_id="m-1",
                            tool_calls=TOOL_CALLS, turn_end="stopped", stop_notes=notes)
        return monitor.get_message_history()[-1]

    with_it = add(_monitor(tmp_path / "a", monkeypatch), NOTES)
    without = add(_monitor(tmp_path / "b", monkeypatch), None)
    assert with_it["stop_notes"] == NOTES and "stop_notes" not in without
    for field in ("content_hash", "tool_calls_digest", "chain_hash"):
        assert with_it[field] == without[field], field
    assert with_it["signature"] and with_it["preimage_version"] == without["preimage_version"]
    monitor = _monitor(tmp_path / "c", monkeypatch)
    stored = add(monitor, NOTES)
    assert monitor._recompute_hash(stored, monitor.conversation_id) == stored["content_hash"]
    # The preimage takes no such field at all.
    assert "stop_notes" not in inspect.signature(message_content_hash).parameters


def test_a_history_sync_does_not_carry_them_and_a_peers_record_is_stored_without_them(tmp_path, monkeypatch):
    monitor = _monitor(tmp_path, monkeypatch)
    monitor.add_message("assistant", "x", sender_node_id="agent_strip", sender_name="Ark",
                        turn_end="stopped", stop_notes=NOTES)
    exported = monitor.export_history()
    assert exported and all("stop_notes" not in m for m in exported)

    group = _monitor(tmp_path / "g", monkeypatch, cid=GROUP)
    group.merge_history([{"id": "p-1", "role": "user", "content": "from a peer",
                          "timestamp": "2026-11-22T10:00:00+00:00",
                          "sender_node_id": "dpc-node-peer", "stop_notes": NOTES}])
    merged = [m for m in group.get_message_history() if m.get("id") == "p-1"]
    assert merged and "stop_notes" not in merged[0]


def test_the_lines_do_not_change_what_a_record_replays_into_the_prompt():
    rec = _rec(2, "assistant", SUMMARY, turn_end="stopped")
    assert history_body(dict(rec, stop_notes=NOTES)) == history_body(rec)


@pytest.mark.asyncio
async def test_a_group_post_keeps_them_on_the_record_and_the_ui_and_the_signed_post_is_unchanged(
        author, monkeypatch):
    """The same post with and without the lines: the signed post a peer receives is the same
    bytes in every field, and carries no `stop_notes`."""
    node_id, signer = author
    monkeypatch.setattr("uuid.uuid4", lambda: SimpleNamespace(hex="feedfacefeedface0000"))
    stamp = SimpleNamespace(now=lambda tz=None: SimpleNamespace(isoformat=lambda: "2026-11-22T10:00:00+00:00"))
    monkeypatch.setattr("dpc_client_core.service.datetime", stamp)
    posted = {}
    for name, notes in (("with", NOTES), ("without", None)):
        service = _Service(node_id, signer, monkeypatch)
        await CoreService.send_group_agent_message(
            service, GROUP, "Ark", SUMMARY, tool_calls=TOOL_CALLS, turn_end="stopped",
            stopped_by="Mike", stop_notes=notes)
        posted[name] = service
    a, b = posted["with"], posted["without"]

    record = a.monitor.get_message_history()[-1]
    assert record["stop_notes"] == NOTES
    assert "stop_notes" not in b.monitor.get_message_history()[-1]
    ui = [p for n, p in a.local_api.events if n == "group_text_received"][-1]
    assert ui["stop_notes"] == NOTES

    wire_a, wire_b = a.broadcast[-1]["payload"], b.broadcast[-1]["payload"]
    assert "stop_notes" not in wire_a
    # Every field but the signature is the same bytes (a signature is salted, so two signings
    # of one preimage differ); the hash it signs is the same, and both verify.
    assert {k: v for k, v in wire_a.items() if k != "signature"} ==         {k: v for k, v in wire_b.items() if k != "signature"}
    assert wire_a["content_hash"] == wire_b["content_hash"] and wire_a["content_hash"]
    for wire in (wire_a, wire_b):
        _, verdict, _ = GroupTextHandler(service=None)._authenticate_author(node_id, wire)
        assert verdict == "verified"


# =============================================================================================
# 3. Telegram hears the press
# =============================================================================================

TG = f"⏹ Stop pressed by Mike {DASH} writing a summary of where it stopped"
TG_KILL = f"⏹ Kill pressed by Mike {DASH} ended at once, no summary"
TG_EARLY = f"⏹ Stopped before the first step {DASH} nothing was done"


def _told(bridge):
    return [(c.args[0], c.args[1]) for c in bridge.send_markdown.await_args_list]


async def _serving_run(quiet=0.02):
    bridge, agent, manager = tg._setup(quiet=quiet)
    agent.until_stopped = True
    task = tg._send(bridge, tg._update(text="long job"))
    await asyncio.wait_for(agent.started.wait(), 2)
    manager._kill_events[tg.CONV_A] = asyncio.Event()  # the run's own, as the manager registers it
    return bridge, agent, manager, task


@pytest.mark.asyncio
async def test_a_stop_pressed_on_the_desktop_tells_the_chat_the_turn_answers():
    bridge, agent, manager, task = await _serving_run()

    assert manager.interrupt(tg.CONV_A, who="Mike") is True
    await tg._finish([task])
    await asyncio.sleep(0.05)

    assert _told(bridge) == [(tg.CHAT_A, TG)]


@pytest.mark.asyncio
async def test_a_kill_is_told_too_and_a_repeated_press_of_either_sends_nothing():
    bridge, agent, manager, task = await _serving_run()

    manager.interrupt(tg.CONV_A, who="Mike")
    manager.interrupt(tg.CONV_A, who="Mike")
    manager.interrupt(tg.CONV_A, kill=True, who="Mike")
    manager.interrupt(tg.CONV_A, kill=True, who="Mike")
    await tg._finish([task])
    await asyncio.sleep(0.05)

    assert _told(bridge) == [(tg.CHAT_A, TG), (tg.CHAT_A, TG_KILL)]


@pytest.mark.asyncio
async def test_a_stop_before_the_first_step_is_told_from_the_loops_own_line():
    bridge, agent, manager, task = await _serving_run()

    manager.interrupt(tg.CONV_A, who="Mike")
    # the loop, finding the Stop before its first round, says so on the progress callback
    manager._emit_progress(stop_note_text("before_round_1"), tg.CONV_A, round=1, stop_note="before_round_1")
    manager._emit_progress(stop_note_text("summary"), tg.CONV_A, stop_note="summary")
    await tg._finish([task])
    await asyncio.sleep(0.05)

    assert [t for _, t in _told(bridge)] == [TG, TG_EARLY], "the summary line is the panel's alone"


@pytest.mark.asyncio
async def test_a_turn_the_bot_does_not_serve_tells_no_chat():
    bridge, agent, manager = tg._setup()
    manager._interrupt_events[tg.AGENT_ID] = asyncio.Event()  # the desktop's own turn
    manager._kill_events[tg.AGENT_ID] = asyncio.Event()
    manager.interrupt(tg.AGENT_ID, who="Mike")
    await asyncio.sleep(0.05)
    assert _told(bridge) == []


@pytest.mark.asyncio
async def test_the_chat_is_not_told_once_its_run_is_over():
    bridge, agent, manager, task = await _serving_run()
    manager.interrupt(tg.CONV_A, who="Mike")
    await tg._finish([task])
    assert bridge._serving == {}
    bridge.send_markdown.reset_mock()
    manager._interrupt_events[tg.CONV_A] = asyncio.Event()  # a later desktop turn in the same chat
    manager.interrupt(tg.CONV_A, who="Mike")
    await asyncio.sleep(0.05)
    assert _told(bridge) == []


@pytest.mark.asyncio
async def test_the_stop_command_reply_is_the_line_and_says_it_once():
    bridge, agent, manager, task = await _serving_run()

    cmd = tg._update(text="/stop")
    cmd.effective_user.full_name = "Mike"
    await bridge._handle_stop_command(cmd, tg._ctx())
    again = tg._update(text="/stop")
    again.effective_user.full_name = "Mike"
    await bridge._handle_stop_command(again, tg._ctx())
    await tg._finish([task])
    await asyncio.sleep(0.05)

    assert tg._replies(cmd) == [f"⏹ Stop pressed by Mike (Telegram) {DASH} writing a summary of where it stopped"
                                "; no messages were waiting to be sent."]
    # the repeat is a reply to a command, plain, and the bot sends nothing besides
    assert tg._replies(again) == ["⏹ Stopped the agent's running turn; no messages were waiting to be sent."]
    assert _told(bridge) == []


@pytest.mark.asyncio
async def test_a_kill_from_the_desktop_after_a_stop_command_is_told_in_the_chat():
    bridge, agent, manager, task = await _serving_run()
    cmd = tg._update(text="/stop")
    cmd.effective_user.full_name = "Mike"
    await bridge._handle_stop_command(cmd, tg._ctx())
    manager.interrupt(tg.CONV_A, kill=True, who="Mike")
    await tg._finish([task])
    await asyncio.sleep(0.05)
    assert _told(bridge) == [(tg.CHAT_A, TG_KILL)]


@pytest.mark.asyncio
async def test_the_notices_go_through_the_events_rate_limit_and_the_bridges_own_send():
    bridge, agent, manager, task = await _serving_run()
    bridge.rate_limit.max_events_per_minute = 1
    bridge._check_rate_limit("something_else")  # the minute's one event is used
    manager.interrupt(tg.CONV_A, who="Mike")
    await tg._finish([task])
    await asyncio.sleep(0.05)
    assert _told(bridge) == []

    bridge2, agent2, manager2, task2 = await _serving_run()
    assert bridge2.notice_for_stop(tg.CONV_A, "pressed", "Mike") is True
    assert bridge2.notice_for_stop(tg.CONV_A, "summary", "Mike") is False
    assert bridge2.notice_for_stop("telegram-999", "pressed", "Mike") is False
    manager2.interrupt(tg.CONV_A)
    await tg._finish([task2])


def test_the_texts_name_who_and_default_to_the_user():
    from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge as B
    assert B.stop_notice_text("pressed", "Mike") == TG
    assert B.stop_notice_text("kill", "Mike") == TG_KILL
    assert B.stop_notice_text("before_round_1", "Mike") == TG_EARLY
    assert B.stop_notice_text("pressed") == f"⏹ Stop pressed by the user {DASH} writing a summary of where it stopped"
    assert B.stop_notice_text("summary", "Mike") is None


@pytest.mark.asyncio
async def test_the_peer_woken_run_hands_the_presser_and_the_lines_to_the_post_too():
    from dpc_client_core.message_handlers.group_handler import GroupTextHandler as H
    notes = [{"kind": "pressed", "who": "Anna", "ts": "t", "text": "x"}]
    manager = SimpleNamespace(
        process_message=AsyncMock(return_value=SUMMARY),
        take_stop_notes=lambda cid: list(notes),
        _last_used_agent=SimpleNamespace(_last_trace={"turn_end": "stopped"}),
    )
    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={"dpc_agent": SimpleNamespace(get_manager=lambda a: manager)}),
        _get_default_agent_id=lambda: "agent_001", _get_agent_display_name=lambda a: "Ark",
        send_group_agent_message=AsyncMock(),
    )
    handler = H(service=service)
    await handler._invoke_agent("group-1", "go", "Mike")
    kw = service.send_group_agent_message.await_args.kwargs
    assert kw["turn_end"] == "stopped" and kw["stopped_by"] == "Anna" and kw["stop_notes"] == notes
