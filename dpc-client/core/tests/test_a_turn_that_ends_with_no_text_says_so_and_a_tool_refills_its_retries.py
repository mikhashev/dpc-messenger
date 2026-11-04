"""A round with reasoning and no text is retried, and a tool that ran refills those retries; a turn
that still has nothing to say ends with a note, never in silence.

Live, 2026-11-04: Johnny's turn of 32 rounds had three empty rounds retried with tool rounds between
them, and the fourth ended it with an empty response. Nothing reached the group, whose readers skip
an empty text. Why the model reasons without answering is not known. The note is stored with
`turn_end` "guard", so it wakes nobody and stays out of knowledge; the reasoning is never posted.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from dpc_client_core.dpc_agent.loop import MAX_EMPTY_ROUNDS_PER_TURN, USER_STOP_SUMMARY_REQUEST, run_llm_loop
from tests.test_a_group_turn_that_ended_without_an_answer_posts_a_notice_and_wakes_nobody import (
    GROUP,
    _Manager,
    _posted,
    _Service,
    _settled,
    _toasts,
)
from tests.test_a_turn_that_ends_without_an_answer_is_saved_with_why import (
    CID,
    _FakeAgent,
    _manager,
    _monitor,
    _on_disk,
)

USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
REASONING = "My private reasoning about the owner's files."
EMPTY = {"content": "", "thinking": REASONING}
ANSWER = "The answer."
WRITTEN = "<tool_call>\n<function=t>\n<parameter=a>\nb\n</parameter>\n</function>\n</tool_call>"
NO_ANSWER = "⚠️ No answer: the agent's model returned no text, so the turn ended here without a reply."


def _tool(n):
    return {"content": "", "tool_calls_native": True, "tool_calls": [
        {"id": f"c{n}", "type": "function", "function": {"name": "t", "arguments": json.dumps({"n": n})}}]}


class _Script:
    """Answers in order; the last answer repeats, and a loop with no bound fails here."""

    def __init__(self, *answers, usage=USAGE):
        self.answers = list(answers)
        self.requests = []
        self.usage = usage

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([dict(m) for m in messages])
        assert len(self.requests) < 80, "the loop did not stop"
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return dict(answer), dict(self.usage)


class _Tools:
    _ctx = None

    def __init__(self):
        self.executed = []

    def schemas(self, core_only=False, include_restricted=False):
        return TOOLS

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        self.executed.append(args)
        return "same"


def _loop(tmp_path, monkeypatch, llm, tools=None, **kwargs):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    return asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=tools or _Tools(), llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t", **kwargs,
    ))


# --- the refill ---------------------------------------------------------------------------

def test_a_fourth_empty_round_with_tools_between_them_does_not_end_the_turn(tmp_path, monkeypatch):
    """The shape of the live turn."""
    llm = _Script(_tool(1), EMPTY, _tool(2), EMPTY, _tool(3), EMPTY, _tool(4), EMPTY, {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and "turn_end" not in trace and len(llm.requests) == 9


def test_three_empty_rounds_in_a_row_are_still_retried_and_a_fourth_ends_the_turn(tmp_path, monkeypatch):
    llm = _Script(EMPTY, EMPTY, EMPTY, EMPTY, {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(llm.requests) == 4 and text.startswith(NO_ANSWER) and trace["turn_end"] == "guard"


def test_a_tool_refills_all_three_retries(tmp_path, monkeypatch):
    llm = _Script(EMPTY, EMPTY, EMPTY, _tool(1), EMPTY, EMPTY, EMPTY, {"content": ANSWER})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and len(llm.requests) == 8


# --- what bounds it -------------------------------------------------------------------------

def test_an_empty_then_tool_ping_pong_ends_at_the_turns_cap(tmp_path, monkeypatch):
    answers = []
    for n in range(40):
        answers += [EMPTY, _tool(n)]
    llm = _Script(*answers)
    tools = _Tools()
    text, usage, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert text.startswith(NO_ANSWER) and trace["turn_end"] == "guard"
    # Each retried empty round and the tool after it, then the empty round past the cap.
    assert len(llm.requests) == 2 * MAX_EMPTY_ROUNDS_PER_TURN + 1
    assert len(tools.executed) == MAX_EMPTY_ROUNDS_PER_TURN
    assert usage["rounds"] == len(llm.requests)


def test_the_round_limit_stands_above_the_refill(tmp_path, monkeypatch):
    answers = []
    for n in range(40):
        answers += [EMPTY, _tool(n)]
    llm = _Script(*answers, EMPTY)
    text, _, trace = _loop(tmp_path, monkeypatch, llm, max_rounds=5)
    assert len(llm.requests) < 2 * MAX_EMPTY_ROUNDS_PER_TURN
    assert any(str(m.get("content")).startswith("[ROUND_LIMIT]") for m in llm.requests[-1])
    assert not text.startswith("⚠️ No answer")


# --- the note -------------------------------------------------------------------------------

def test_the_note_posts_no_reasoning(tmp_path, monkeypatch):
    text, usage, _ = _loop(tmp_path, monkeypatch, _Script(EMPTY))
    assert REASONING not in text
    assert text == NO_ANSWER + " Ask again to get one."
    # Beside the text, where a 1:1 record keeps it for its owner.
    assert usage["thinking"] == REASONING


def test_the_note_says_the_tools_that_ran_did_run(tmp_path, monkeypatch):
    text, _, _ = _loop(tmp_path, monkeypatch, _Script(_tool(1), EMPTY))
    assert text.startswith(NO_ANSWER)
    assert "The tools it called before that did run" in text and "Ask again to get one" not in text


def test_words_kept_before_a_retry_stay_under_the_note(tmp_path, monkeypatch):
    llm = _Script({"content": "Short report.\n\n" + WRITTEN}, EMPTY)
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith(NO_ANSWER) and text.endswith("\n\nShort report.") and trace["turn_end"] == "guard"


def test_a_reply_of_nothing_but_the_history_marker_ends_the_same_way(tmp_path, monkeypatch):
    text, _, trace = _loop(tmp_path, monkeypatch, _Script({"content": "[#74 | 06:42:57 | Johnny]"}))
    assert text.startswith(NO_ANSWER) and "[#74" not in text and trace["turn_end"] == "guard"


# --- unchanged --------------------------------------------------------------------------------

def test_the_diagnosis_stays_in_the_log(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        _loop(tmp_path, monkeypatch, _Script(EMPTY))
    lines = [r.getMessage() for r in caplog.records]
    assert sum("retry" in line and "thinking-budget" in line for line in lines) == 3
    assert any("after 3 retries" in line and "thinking-budget" in line for line in lines)


def test_a_reply_with_no_usage_at_all_stays_a_failure(tmp_path, monkeypatch):
    llm = _Script({"content": ""}, usage={"prompt_tokens": 0, "completion_tokens": 0})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(llm.requests) == 1 and trace["turn_end"] == "failed" and "dropped the request" in text


def test_a_stop_during_the_empty_rounds_is_still_the_persons(tmp_path, monkeypatch):
    stop = asyncio.Event()

    class _StopsOnTheEmptyRound(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            result = await super().chat(messages, tools=tools, **kwargs)
            if len(self.requests) == 2:
                stop.set()
            return result

    llm = _StopsOnTheEmptyRound(_tool(1), EMPTY, {"content": "Where I stopped."})
    text, _, trace = _loop(tmp_path, monkeypatch, llm, stop_event=stop)
    assert trace["turn_end"] == "stopped" and text == "Where I stopped."
    assert llm.requests[-1][-1]["content"] == USER_STOP_SUMMARY_REQUEST


def test_a_guards_empty_finalising_answer_stays_the_guards_message(tmp_path, monkeypatch):
    text, _, trace = _loop(tmp_path, monkeypatch, _Script(EMPTY), max_rounds=0)
    assert text.startswith("[ROUND_LIMIT]") and trace["turn_end"] == "guard"


# --- the readers ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_group_reads_the_note_with_no_toast_and_nobody_is_woken(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    text, _, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(),
        llm=_Script({"content": "Over to @Muse.\n\n" + WRITTEN}, EMPTY),
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t",
    )
    service = _Service(_Manager(text, trace))

    await service._invoke_agent_in_group(GROUP, "hello", "Mike", "agent_001")
    await _settled()

    assert _posted(service)["text"] == text and text.startswith(NO_ANSWER)
    assert REASONING not in str(service.broadcast)
    assert _toasts(service) == [] and service.woken == []


@pytest.mark.asyncio
async def test_the_chat_record_holds_the_note_and_stays_out_of_knowledge(tmp_path, monkeypatch, signing_identity):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    text, _, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(), llm=_Script(EMPTY),
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t",
    )
    monitor = _monitor(tmp_path)
    await _manager(_FakeAgent(text, trace), monitor)._process_message_guarded("hello", CID)

    _, record = _on_disk(tmp_path)
    assert record["content"] == text and record["turn_end"] == "guard"
    assert [m.text for m in monitor.full_conversation] == ["hello"]
