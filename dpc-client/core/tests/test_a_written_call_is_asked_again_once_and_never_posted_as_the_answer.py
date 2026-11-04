"""A final answer that is itself a tool call is not an answer: the loop asks once
more with the reason, and a call still written ends the turn with a note, never
with the markup.

Live, 2026-11-03: round 32 of Johnny's turn on `strata flash-next` wrote the call
below as text. The Strata launcher refused it (`strata_rejected_tool_calls`) and
returned it as content, and the loop posted it to the group as his answer: 37
minutes of research with the reply unwritten. The same launcher line stood on
rounds 25-27, where a valid call in the same reply carried the turn on and the
refused one vanished. The fixtures are shapes from the stored history, words reduced.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.guards import ResearchLimitGuard
from dpc_client_core.dpc_agent.hooks import HookRegistry
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.guards import BudgetLimitGuard  # noqa: F401  (a stop like any other)
from dpc_client_core.dpc_agent.loop import (
    CallKilled,
    MAX_WRITTEN_CALL_RETRIES,
    USER_STOP_SUMMARY_REQUEST,
    _answer_without_unrun_calls,
    _finalize_after_guard_stop,
    _finalize_after_user_stop,
    _written_tool_calls,
    run_llm_loop,
)
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.managers.llama_server_supervisor import (
    CardQueueRefusal,
    TakenOutOfTheCardQueue,
    card_held_for,
    current_card_hold,
)
from dpc_client_core.node_ledger import NodeLedger
from dpc_protocol.protocol import REFUSAL_NOT_ALLOWED, PeerRefused
from dpc_client_core.providers import LlamaServerProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

# The name fused with the first parameter's, a `</parameter>` closing a tag never opened.
JOHNNY = (
    "<tool_call>\n<function=search_in_file=context_lines>\n2\n</parameter>\n"
    "<parameter=file_path>\nC:\\Users\\someone\\dpc-hub\\dpc_hub\\main.py\n</parameter>\n"
    "<parameter=pattern>\nauthlib\n</parameter>\n</function>\n</tool_call>"
)
WELL_FORMED = "<tool_call>\n<function=update_scratchpad>\n<parameter=mode>\nappend\n</parameter>\n</function>\n</tool_call>"
ANSWER = "Authlib is pinned in dpc-hub; the advisory does not reach the client."
LONG = "A long paragraph of analysis. " * 20

# (shape, the tool the note must name): whole messages and prose followed by a call.
WRITTEN = [
    (JOHNNY, "search_in_file"),
    (WELL_FORMED, "update_scratchpad"),
    ("<tool_call>\n<function=exec>\n<parameter=shell>\nx\ny\n</parameter>\n</function>\n</tool_call>", "exec"),
    ("<tool_call>\n=update_scratchpad>\n<parameter=action>\nwrite\n</parameter>\n</function>\n</tool_call>",
     "update_scratchpad"),
    ("<tool_call>\n<function=run_file_placeholder>\n<parameter=path>\nx\n</parameter>\n</function>\n</tool_call>",
     "run_file_placeholder"),
    ("Searching for the text.\n\n<tool_call>\n<function=browse_page>\n<parameter=url>\nx\n</parameter>\n"
     "</function>\n</tool_call>", "browse_page"),
    ("Let me look.\n\n<tool_call>knowledge_read<arg_key>topic</arg_key><arg_value>x</arg_value></tool_call>",
     "knowledge_read"),
    # Cut by max_tokens: no closer, and what follows is the call's argument.
    ("<tool_call>\n<function=write_file>\n<parameter=content>\nthe text ran out here", "write_file"),
    ("<tool_call>\n<function=write_file>", "write_file"),
    # The runtime's history marker in front of it.
    ("[#127 | 16:36:25 | Forge] <tool_call>\n<function=list_dir>\n</function>\n</tool_call>", "list_dir"),
    # Fenced: JSON that parses, JSON max_tokens cut, and something that is not JSON.
    ('```tool_call\n{"name": "read_file", "arguments": {"path": "x"}}\n```', "read_file"),
    ('```tool_call\n{"name": "update_scratchpad", "arguments": {"content": "notes', "update_scratchpad"),
    ("Done with that.\n\n```tool_call\nnot json at all\n```", "an unnamed tool"),
    ('```tool_call\n{"name": "git_diff", "arguments": {"x"\n```\n\n[TOOL RESULT: call_1]\nmade up', "git_diff"),
    # The same opener spelled with a space, and an engine's stop token left behind the block.
    ("<tool_call >\n<function=read_file>\n</function>\n</tool_call>", "read_file"),
    (WELL_FORMED + "\n<|im_end|>", "update_scratchpad"),
    (WELL_FORMED + "\n</tool_call>", "update_scratchpad"),
    # A closing word inside a value is the value's, whatever follows it.
    ("<tool_call>\n<function=write_file>\n<parameter=content>\nthe closer is </tool_call> and more\n"
     "</parameter>\n</function>\n</tool_call>", "write_file"),
    # Three spaces are not yet a code block.
    ("   " + WELL_FORMED, "update_scratchpad"),
    # DSML, the two shapes in the history.
    ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="run_shell">\n<｜｜DSML｜｜ parameter name="c" string="true">x'
     '</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>', "run_shell"),
    ('<｜DSML｜function_calls>\n<｜DSML｜invoke name="write_file">\n<｜DSML｜parameter name="c" string="true">x'
     '</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜function_calls>', "write_file"),
]

# Prose that talks about the format: stored as written, one model call, no note.
PROSE = [
    "Plain answer with no markup at all.",
    "A call opens with `<tool_call>` and then `<function=NAME>`.",
    "The template form:\n\n```xml\n" + WELL_FORMED + "\n```\n\nThat is what the launcher reads.",
    "Quoted in a fence with nothing after it:\n\n```\n" + JOHNNY + "\n```",
    # A fence left open: the tag still stands in code, with nothing after the block.
    "The form, in a fence nobody closed:\n\n```\n" + WELL_FORMED,
    "    " + WELL_FORMED.replace("\n", "\n    "),
    "I would use a <tool_call> block for that.",
    "<tool_call> is the tag the template opens with.",
    "<tool_call>\n<function=write_file> is how a call opens, and the name comes next.",
    "The model wrote <tool_call><function=foo> and stopped there",
    "The model wrote " + WELL_FORMED + " instead of calling it.",
    # A block on its own lines with an explanation after it is a quote.
    "The stored text was:\n" + JOHNNY + "\nThe name fused with the first parameter's.",
    # The shape of Ark's 2026-05-12 reply: a stray tag, then the real answer.
    "Reading the code.\n\n<tool_call>read_file</tool_call>\n\nI read the whole pipeline; here is what it does.",
    # The first block's own closer rules: analysis behind it is an answer, whatever closes the text.
    WELL_FORMED + "\n\n" + LONG + "\n\n" + WELL_FORMED,
    WELL_FORMED + "\n\n" + LONG + "\n\nThe closer is </tool_call>",
    # No closer, and prose outside any value.
    "<tool_call>\n<function=x=y>\n2\n</parameter>\n" + LONG,
    # A tag named in a sentence, with no value on its own line.
    "<tool_call>\n<function=NAME>\n<parameter=KEY> is the form the template teaches. " + LONG,
    "<tool_call>\n<function=x> stands first; </parameter> closes. " + LONG,
    "<tool_call>\n<function=x> stands first, and this closes it: </tool_call>",
    # A tab is a Markdown code block.
    LONG + "\n\n\t" + WELL_FORMED.replace("\n", "\n\t"),
]


@pytest.mark.parametrize("content,name", WRITTEN)
def test_each_stored_shape_is_a_written_call(content, name):
    names, _ = _written_tool_calls(content)
    assert names == [name]
    text = _answer_without_unrun_calls(content, {})
    assert text.startswith("⚠️ Not done") and f"`{name}` was never executed" in text
    assert "<tool_call" not in text and "```tool_call" not in text and "DSML" not in text
    assert "parameter" not in text.split("ask again")[-1]


@pytest.mark.parametrize("content", PROSE)
def test_prose_about_the_format_is_not_a_written_call(content):
    assert _written_tool_calls(content) == ([], content)
    trace = {}
    assert _answer_without_unrun_calls(content, trace) == content and trace == {}


def test_a_call_fence_quoted_inside_another_fence_is_not_a_written_call():
    quoted = "The raw output:\n```\n```tool_call\nnot json\n```\n"
    assert _written_tool_calls(quoted) == ([], quoted)


def test_two_blocks_one_after_the_other_are_both_written_calls():
    names, prose = _written_tool_calls("Both.\n\n" + WELL_FORMED + "\n\n" + JOHNNY)
    assert names == ["update_scratchpad", "search_in_file"] and prose == "Both."


def test_a_fence_with_words_after_it_is_a_written_call_and_the_words_stay():
    """The role cut would eat everything from the fence on, the answer going empty with no note."""
    names, prose = _written_tool_calls("Before.\n\n```tool_call\nnot json\n```\n\nNow waiting.")
    assert names == ["an unnamed tool"] and prose == "Before.\n\nNow waiting."
    names, prose = _written_tool_calls(
        '```tool_call\n{"name": "a", "arguments": {\n```\n\n```tool_call\n{"name": "b", "arguments": {\n```\n\nDone.')
    assert names == ["a", "b"] and prose == "Done."


def test_prose_in_front_of_the_call_stays_under_the_note():
    names, prose = _written_tool_calls("Searching for the text.\n\n" + WELL_FORMED)
    assert names == ["update_scratchpad"] and prose == "Searching for the text."


# --- the loop, with a provider double ---------------------------------------------

USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
TOOLS = [{"type": "function", "function": {"name": "search_in_file", "parameters": {}}}]


def _valid(call_id="v1"):
    return {"id": call_id, "type": "function", "function": {"name": "search_in_file", "arguments": "{}"}}


def _native(content="", *calls, **fields):
    """A reply with native calls, as the adapter marks them."""
    return {"content": content, "tool_calls": list(calls or [_valid()]), "tool_calls_native": True, **fields}


class _Script:
    """Answers in order; the last answer repeats, so a loop with no bound would hang here
    until the call budget below fails it."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools,
                              "tool_choice": kwargs.get("tool_choice"),
                              "on_stream_chunk": kwargs.get("on_stream_chunk")})
        assert len(self.requests) < 40, "the loop did not stop"
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return dict(answer), dict(USAGE)


class _Tools:
    _ctx = None

    def __init__(self):
        self.executed = []

    def schemas(self, core_only=False, include_restricted=False):
        return TOOLS

    def get_timeout(self, name):
        return 5

    def execute(self, name, args, ctx=None):
        self.executed.append(name)
        return "found"


def _loop(tmp_path, monkeypatch, llm, tools=None, **kwargs):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    return asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "what about authlib?"}], tools=tools or _Tools(), llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t", **kwargs,
    ))


def test_a_written_call_is_asked_again_and_the_retrys_words_are_the_answer(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY}, {"content": ANSWER})
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER
    assert len(llm.requests) == 2
    assert "turn_end" not in trace and "unexecuted_tool_calls" not in trace
    (retry,) = trace["written_call_retries"]
    assert (retry["round"], retry["tools"], retry["rejected"], retry["chars"]) == (
        1, ["search_in_file"], [], len(JOHNNY))
    # The reason says what happened, and the broken form is not shown back to be copied.
    reason = llm.requests[1]["messages"][-1]
    assert reason["role"] == "user"
    assert "`search_in_file`" in reason["content"] and "not executed" in reason["content"].lower()
    assert "declared" in reason["content"] and "in words" in reason["content"]
    assert not any("<tool_call" in str(m.get("content")) for m in llm.requests[1]["messages"])
    # A retry is a round: it costs what a round costs, and is counted as one.
    assert usage["rounds"] == 2 and usage["prompt_tokens"] == 20


def test_a_retry_that_still_writes_a_call_ends_the_turn_with_the_note(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY})
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(llm.requests) == 2, "one retry since the last tool ran"
    assert text.startswith("⚠️ Not done") and "`search_in_file` was never executed" in text
    assert "without its answer" in text
    assert "<tool_call" not in text and "</parameter>" not in text
    assert trace["turn_end"] == "guard"
    kept_at = trace["unexecuted_tool_calls"][0]["kept_at"]
    assert f"kept at `{kept_at}`" in text
    assert (tmp_path / kept_at).read_bytes() == JOHNNY.encode("utf-8")
    assert usage["rounds"] == 2
    rows = [json.loads(line) for line in (tmp_path / "logs" / "reasoning.jsonl").read_text("utf-8").splitlines()]
    assert [row["answered"] for row in rows] == [False, False]


def test_prose_in_front_of_the_call_goes_back_as_the_models_own_turn(tmp_path, monkeypatch):
    llm = _Script({"content": "Searching.\n\n" + WELL_FORMED}, {"content": ANSWER})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "Searching.\n\n" + ANSWER
    said, reason = llm.requests[1]["messages"][-2:]
    assert said == {"role": "assistant", "content": "Searching."}
    assert reason["role"] == "user" and "`update_scratchpad`" in reason["content"]


@pytest.mark.parametrize("content", PROSE)
def test_prose_about_the_format_ends_the_turn_as_an_answer(tmp_path, monkeypatch, content):
    llm = _Script({"content": content})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.strip() == content.strip() and len(llm.requests) == 1
    assert "turn_end" not in trace and "written_call_retries" not in trace


def test_a_launcher_flag_on_prose_does_not_turn_the_prose_into_a_call(tmp_path, monkeypatch):
    """The launcher counts a mid-sentence opener outside code, so its field can stand on a
    sentence about the format. The sentence is still the answer."""
    content = "The model wrote " + WELL_FORMED + " instead of calling it."
    llm = _Script({"content": content, "rejected_tool_calls": ["update_scratchpad"]})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == content and len(llm.requests) == 1
    assert trace["rejected_tool_calls"] == [{"round": 1, "tools": ["update_scratchpad"]}]


def test_the_retry_names_the_call_as_the_launcher_read_it(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY, "rejected_tool_calls": ["search_in_file=context_lines"]},
                  {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER
    assert "`search_in_file=context_lines`" in llm.requests[1]["messages"][-1]["content"]
    assert trace["written_call_retries"][0]["rejected"] == ["search_in_file=context_lines"]


# --- the bound ----------------------------------------------------------------------

def test_work_between_two_written_calls_earns_another_retry_up_to_the_turns_cap(tmp_path, monkeypatch):
    """Written, retry makes a valid call, written again... A tool that ran earns another retry, up
    to the cap."""
    answers = []
    for n in range(MAX_WRITTEN_CALL_RETRIES + 3):
        answers += [{"content": JOHNNY}, _native("", _valid(f"v{n}"))]
    tools = _Tools()
    llm = _Script(*answers)
    text, usage, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert len(trace["written_call_retries"]) == MAX_WRITTEN_CALL_RETRIES
    assert len(tools.executed) == MAX_WRITTEN_CALL_RETRIES
    # Each retry and its call, then the written call past the cap, which gets none.
    assert len(llm.requests) == 2 * MAX_WRITTEN_CALL_RETRIES + 1
    assert text.startswith("⚠️ Not done") and trace["turn_end"] == "guard"
    assert usage["rounds"] == len(llm.requests)


def test_the_round_limit_stands_above_the_retry(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY})
    text, _, trace = _loop(tmp_path, monkeypatch, llm, max_rounds=1)
    assert "<tool_call" not in text and text.startswith("⚠️ Not done")
    assert trace["turn_end"] == "guard"
    # The round, its retry refused by the limit, the finalising call and its one re-ask.
    assert [r["tool_choice"] for r in llm.requests] == [None, None, "none"]


def test_the_empty_answer_retries_and_the_written_call_retry_count_apart(tmp_path, monkeypatch):
    llm = _Script({"content": ""}, {"content": ""}, {"content": ""},
                  {"content": JOHNNY},
                  {"content": ANSWER})
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and len(llm.requests) == 5
    assert len(trace["written_call_retries"]) == 1
    # And the other way round: an empty reply to the retry is the empty path's, with the reason still sent.
    llm = _Script({"content": JOHNNY}, {"content": ""}, {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and len(llm.requests) == 3
    assert len(trace["written_call_retries"]) == 1
    assert "`search_in_file`" in llm.requests[2]["messages"][-1]["content"]


def test_a_retry_round_stands_on_the_card_as_any_round_does(tmp_path, monkeypatch):
    """The real hold and the real `card_hold_follows`: both model calls stand on the alias, in one hold."""
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    alias = "strata flash-next"
    seen = []

    class _OnTheAlias(_Script):
        def local_alias(self):
            return alias

        async def chat(self, messages, tools=None, **kwargs):
            hold = current_card_hold()
            seen.append((id(hold), hold.alias))
            return await super().chat(messages, tools=tools, **kwargs)

    llm = _OnTheAlias({"content": JOHNNY}, {"content": ANSWER})

    async def run():
        async with card_held_for(alias, "accepted_request", agent_id="a", conversation_id="c"):
            result = await run_llm_loop(
                messages=[{"role": "user", "content": "q"}], tools=_Tools(), llm=llm, agent_root=tmp_path,
                emit_progress=lambda *a, **k: None, task_id="t")
        assert current_card_hold() is None
        return result

    text, _, _ = asyncio.run(run())
    assert text == ANSWER and len(seen) == 2
    assert seen[0] == seen[1] and seen[0][1] == alias


# --- a refused call beside a valid one --------------------------------------------

def test_a_call_refused_beside_a_valid_one_is_told_and_not_replayed(tmp_path, monkeypatch):
    tools = _Tools()
    llm = _Script(
        _native("Checking both.\n\n" + JOHNNY, rejected_tool_calls=["search_in_file=context_lines"]),
        {"content": ANSWER},
    )
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert text == ANSWER and tools.executed == ["search_in_file"]
    sent = llm.requests[1]["messages"]
    assert not any("<tool_call" in str(m.get("content")) for m in sent)
    assistant, tool_result, told = sent[-3:]
    assert assistant["role"] == "assistant" and assistant["content"] == "Checking both."
    assert tool_result["role"] == "tool"
    assert told["role"] == "user" and "`search_in_file=context_lines`" in told["content"]
    assert "not executed" in told["content"].lower()
    assert trace["written_calls_beside"] == [{"round": 1, "tools": ["search_in_file=context_lines"]}]
    assert "written_call_retries" not in trace
    assert "<tool_call" not in trace["accumulated_tool_calls"][0]["round_text"]


def test_a_call_written_beside_a_valid_one_is_told_without_the_launchers_field(tmp_path, monkeypatch):
    tools = _Tools()
    llm = _Script(_native(WELL_FORMED), {"content": ANSWER})
    text, _, _ = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert text == ANSWER and tools.executed == ["search_in_file"]
    assert "`update_scratchpad`" in llm.requests[1]["messages"][-1]["content"]


def test_a_call_the_adapter_read_out_of_the_text_is_run_and_not_reported_as_refused(tmp_path, monkeypatch):
    """The fenced JSON `_parse_tool_calls` reads: the call in the text IS the call that ran."""
    tools = _Tools()

    class _Parsing(_Script):
        def _parse_tool_calls(self, content):
            return [_valid()] if "```tool_call" in content else []

    llm = _Parsing({"content": '```tool_call\n{"name": "search_in_file", "arguments": {}}\n```'},
                   {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert text == ANSWER and tools.executed == ["search_in_file"]
    assert "written_calls_beside" not in trace and "written_call_retries" not in trace
    assert llm.requests[1]["messages"][-1]["role"] == "tool"


# --- the finalising calls ------------------------------------------------------------

def _hooks():
    hooks = HookRegistry()
    guard = ResearchLimitGuard()
    hooks.register(guard)
    hooks.last_triggered = guard
    return hooks


def _after_guard(llm, dispatch=None):
    return asyncio.run(_finalize_after_guard_stop(
        _hooks(), [{"role": "user", "content": "q"}], llm, None, "c", {}, {},
        fallback_reason="stopped", tools=TOOLS, execute_tool_calls=dispatch,
    ))


def test_a_finalising_call_that_writes_a_call_is_asked_once_more_for_words():
    llm = _Script({"content": WELL_FORMED}, {"content": ANSWER})
    text, _, trace = _after_guard(llm)
    assert text == ANSWER and trace["guard_answered"] is True and "turn_end" not in trace
    assert [r["tool_choice"] for r in llm.requests] == [None, "none"]
    reason = llm.requests[1]["messages"][-1]
    assert "`update_scratchpad`" in reason["content"] and "in words" in reason["content"]
    assert "declared form" not in reason["content"], "no tool will run on this call"


def test_the_live_sequence_of_october_ends_with_the_note_after_one_more_ask():
    """A call run once, `tool_choice=none`, the call written as text, asked again, written again."""
    async def dispatch(target, calls, reasoning):
        for tc in calls:
            target.append({"role": "tool", "tool_call_id": tc["id"], "content": "ok"})
        return True

    llm = _Script(_native(""), {"content": WELL_FORMED})
    text, usage, trace = _after_guard(llm, dispatch)
    assert [r["tool_choice"] for r in llm.requests] == [None, "none", "none"]
    assert usage.get("rounds", 0) == 0 and usage["total_tokens"] == 3 * USAGE["total_tokens"]
    assert text.startswith("⚠️ Not done") and "`update_scratchpad`" in text and "<tool_call" not in text
    assert trace["turn_end"] == "guard" and "guard_answered" not in trace


def test_a_stop_during_the_one_more_ask_ends_the_turn_as_stopped():
    class _StopsOnSecond(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            if self.requests:
                stop.set()
                await asyncio.sleep(5)
            return await super().chat(messages, tools=tools, **kwargs)

    async def run():
        nonlocal stop
        stop = asyncio.Event()
        return await _finalize_after_guard_stop(
            _hooks(), [{"role": "user", "content": "q"}], _StopsOnSecond({"content": WELL_FORMED}), None, "c",
            {}, {}, fallback_reason="stopped", tools=TOOLS, stop_event=stop,
        )

    stop = None
    text, _, trace = asyncio.run(run())
    assert text.startswith("⚠️ Stopped by user") and trace["stopped_by_user"] is True
    assert "<tool_call" not in text


def test_a_finalising_round_that_runs_a_call_does_not_replay_the_one_refused_beside_it():
    async def dispatch(target, calls, reasoning):
        for tc in calls:
            target.append({"role": "tool", "tool_call_id": tc["id"], "content": "ok"})
        return True

    llm = _Script(_native("Saving.\n\n" + JOHNNY), {"content": ANSWER})
    text, _, _ = _after_guard(llm, dispatch)
    assert text == ANSWER
    assert not any("<tool_call" in str(m.get("content")) for m in llm.requests[1]["messages"])
    assert llm.requests[1]["messages"][-2]["content"] == "Saving."


def test_the_summary_after_a_stop_is_replaced_by_the_note_with_no_further_call():
    llm = _Script({"content": JOHNNY})
    text, _, trace = asyncio.run(_finalize_after_user_stop(
        [{"role": "user", "content": "q"}], llm, None, "c", {}, {}, "⚠️ Stopped by user after 3 rounds.",
        tools=TOOLS,
    ))
    assert len(llm.requests) == 1 and llm.requests[0]["messages"][-1]["content"] == USER_STOP_SUMMARY_REQUEST
    assert text.startswith("⚠️ Not done") and "<tool_call" not in text
    assert trace["turn_end"] == "stopped"


# --- the real provider and adapter: the field arrives, and no markup is streamed ----

@pytest.fixture(autouse=True)
def _no_live_supervisors():
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


class _Supervisor:
    port = 8123
    props = {"total_slots": 4}

    async def ensure_running(self):
        return self.props

    def call_slot(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _engine_reply(content, *, rejected=None, calls=None, extra_absent=False):
    """A response as the OpenAI SDK builds it: an unknown top-level field lands in `model_extra`."""
    msg = SimpleNamespace(
        content=content, reasoning_content=None,
        tool_calls=[SimpleNamespace(id=c, function=SimpleNamespace(name="search_in_file", arguments="{}"))
                    for c in calls or []] or None,
    )
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )
    if not extra_absent:
        reply.model_extra = {"strata_rejected_tool_calls": rejected} if rejected is not None else {}
    return reply


def _llm_over_the_real_provider(tmp_path, *replies):
    provider = LlamaServerProvider("local", {"type": "llamacpp_server", "gguf_path": "models/q.gguf"})
    provider.supervisor = _Supervisor()
    bodies = []
    left = list(replies)

    async def create(**params):
        bodies.append(params)
        return left.pop(0)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def _ensure():
        return client

    provider._ensure = _ensure
    manager = SimpleNamespace(token_count_manager=None, providers={"local": provider},
                              agent_provider=None, default_provider="local")
    return DpcLlmAdapter(manager, provider_alias="local", ledger=NodeLedger(tmp_path / "ledger")), bodies


FIELD = [{"name": "search_in_file=context_lines", "reason": "undeclared"}]


def test_johnnys_reply_through_the_real_provider_is_retried_with_the_launchers_name(tmp_path, monkeypatch):
    llm, bodies = _llm_over_the_real_provider(
        tmp_path, _engine_reply(JOHNNY, rejected=FIELD), _engine_reply(ANSWER))
    text, usage, trace = _loop(tmp_path / "agent", monkeypatch, llm)
    assert text == ANSWER and len(bodies) == 2 and usage["rounds"] == 2
    assert trace["written_call_retries"][0]["rejected"] == ["search_in_file=context_lines"]
    assert len(list(NodeLedger(tmp_path / "ledger").rows())) == 2, "the retry leaves its own usage row"
    assert "search_in_file=context_lines" in bodies[1]["messages"][-1]["content"]
    assert not any("<tool_call" in str(m.get("content")) for m in bodies[1]["messages"])
    assert bodies[1]["tools"] == bodies[0]["tools"] and bodies[1]["tool_choice"] == "auto"


def test_the_refused_names_are_in_the_log_with_the_alias(tmp_path, caplog):
    llm, _ = _llm_over_the_real_provider(tmp_path, _engine_reply(JOHNNY, rejected=FIELD))
    with caplog.at_level("WARNING"):
        msg, _ = asyncio.run(llm.chat([{"role": "user", "content": "q"}], tools=TOOLS))
    assert msg["rejected_tool_calls"] == ["search_in_file=context_lines"]
    assert msg["content"] == JOHNNY and "tool_calls" not in msg
    line = next(r.getMessage() for r in caplog.records if "search_in_file=context_lines" in r.getMessage())
    assert "local" in line


@pytest.mark.parametrize("field", [None, [], "bash", [{"reason": "undeclared"}], ["bash"], {"name": "bash"}])
def test_a_reply_without_a_usable_field_carries_no_names(tmp_path, field):
    llm, _ = _llm_over_the_real_provider(tmp_path, _engine_reply("plain", rejected=field))
    msg, _ = asyncio.run(llm.chat([{"role": "user", "content": "q"}], tools=TOOLS))
    assert msg["content"] == "plain" and "rejected_tool_calls" not in msg


def test_a_provider_with_no_field_at_all_still_meets_the_check_on_the_text(tmp_path, monkeypatch):
    """Plain llama-server: no `model_extra` key. The check on the text is all there is."""
    llm, bodies = _llm_over_the_real_provider(
        tmp_path, _engine_reply(JOHNNY, extra_absent=True), _engine_reply(ANSWER, extra_absent=True))
    text, _, trace = _loop(tmp_path / "agent", monkeypatch, llm)
    assert text == ANSWER and len(bodies) == 2
    assert trace["written_call_retries"][0]["rejected"] == []
    assert "`search_in_file`" in bodies[1]["messages"][-1]["content"]


def test_a_written_call_handed_over_as_one_chunk_is_not_streamed(tmp_path, monkeypatch):
    """The tools path hands the whole reply over as one chunk; in a 1:1 chat those chunks
    become `streaming_raw` on the stored record and the UI's live text. A reply streamed by
    the piece is not covered: the next test pins that."""
    chunks = []

    async def on_chunk(chunk, conversation_id):
        chunks.append(chunk)

    llm, _ = _llm_over_the_real_provider(tmp_path, _engine_reply(JOHNNY, rejected=FIELD), _engine_reply(ANSWER))
    text, _, _ = _loop(tmp_path / "agent", monkeypatch, llm, on_stream_chunk=on_chunk, conversation_id="c")
    assert text == ANSWER and chunks == [ANSWER]
    # `emit_stream_chunk` is the callback that both broadcasts and collects, so a chunk dropped
    # before it is on neither the screen nor the stored raw text.
    assert DpcAgentManager._answer_extras(SimpleNamespace(_last_usage={}), text, chunks) == (None, None)


def test_a_written_call_streamed_by_the_piece_reaches_the_live_text_and_not_the_stored_raw(tmp_path, monkeypatch):
    """The text path's stream arrives in pieces, none of them a call, so the live text shows the
    markup: a known gap, pinned so its fix is seen. What is stored is cleaned when the turn ends."""
    chunks = []

    async def on_chunk(chunk, conversation_id):
        chunks.append(chunk)

    class _Stream(_TextProvider):
        async def generate_response_stream(self, prompt, on_chunk=None, conversation_id=None, reasoning_effort=None):
            reply = self.replies.pop(0)
            for at in range(0, len(reply), 7):
                await on_chunk(reply[at:at + 7], conversation_id)
            return reply

    provider = _Stream(WELL_FORMED, ANSWER)
    text, _, _ = _loop(tmp_path / "agent", monkeypatch, _text_path_llm(tmp_path, provider),
                       on_stream_chunk=on_chunk, conversation_id="c")
    assert text == ANSWER and "<tool_call>" in "".join(chunks)
    assert DpcAgentManager._answer_extras(SimpleNamespace(_last_usage={}), text, chunks) == (None, None)


def test_the_stored_raw_text_keeps_what_is_around_a_written_call():
    pieces = ["Before. ", WELL_FORMED[:20], WELL_FORMED[20:], " After."]
    _, raw = DpcAgentManager._answer_extras(SimpleNamespace(_last_usage={}), "The answer.", pieces)
    assert raw == "Before.\n\nAfter."


def test_the_call_beside_a_valid_one_through_the_real_provider(tmp_path, monkeypatch):
    tools = _Tools()
    llm, bodies = _llm_over_the_real_provider(
        tmp_path, _engine_reply(JOHNNY, rejected=FIELD, calls=["v1"]), _engine_reply(ANSWER))
    text, _, trace = _loop(tmp_path / "agent", monkeypatch, llm, tools=tools)
    assert text == ANSWER and tools.executed == ["search_in_file"]
    assert trace["written_calls_beside"][0]["tools"] == ["search_in_file=context_lines"]
    assert not any("<tool_call" in str(m.get("content")) for m in bodies[1]["messages"])


# --- a call that ran is never reported as refused ------------------------------------

class _TextProvider:
    """No `generate_with_tools`: the adapter puts the tools in the prompt and parses the answer,
    as for Anthropic, OpenAI-compatible, Gemini and GigaChat in this tree."""
    alias = "text"
    model = "m"
    config = {"type": "openai_compatible"}

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    async def generate_response(self, prompt, reasoning_effort=None):
        self.prompts.append(prompt)
        return self.replies.pop(0)

    def get_last_usage(self):
        return dict(USAGE)


class _NativeThatFails(_TextProvider):
    async def generate_with_tools(self, messages, tools, system="", **kwargs):
        raise RuntimeError("the native path is down")


def _text_path_llm(tmp_path, provider):
    manager = SimpleNamespace(token_count_manager=None, providers={"text": provider},
                              agent_provider=None, default_provider="text")
    return DpcLlmAdapter(manager, provider_alias="text", ledger=NodeLedger(tmp_path / "ledger"))


FENCED_CALL = '```tool_call\n{"name": "search_in_file", "arguments": {"path": "x"}}\n```'


@pytest.mark.parametrize("provider_type", [_TextProvider, _NativeThatFails])
def test_a_call_the_text_path_read_out_of_the_reply_ran_and_the_model_is_not_told_otherwise(
        tmp_path, monkeypatch, provider_type):
    """On the real adapter `msg["tool_calls"]` is the fence itself. Telling the model that fence was
    not executed invites it to run a write, a shell command or a send twice."""
    tools = _Tools()
    provider = provider_type(FENCED_CALL, ANSWER)
    text, _, trace = _loop(tmp_path / "agent", monkeypatch, _text_path_llm(tmp_path, provider), tools=tools)
    assert text == ANSWER and tools.executed == ["search_in_file"]
    assert "written_calls_beside" not in trace and "written_call_retries" not in trace
    assert "not executed" not in provider.prompts[1] and "as text" not in provider.prompts[1]


def test_only_the_native_paths_mark_their_calls_native(tmp_path):
    """The peer's two branches, through the real route."""
    from tests.test_a_guest_agent_sends_its_tools_natively_where_the_host_serves_them import (
        CALL, TEXT_CALL, TOOLS as PEER_TOOLS, TURNS, _answer, _guest,
    )

    adapter, _, _ = _guest(tmp_path, [_answer(tool_calls=[CALL], finish_reason="tool_calls")], supports_tools=True)
    msg, _ = asyncio.run(adapter.chat(TURNS, tools=PEER_TOOLS))
    assert msg["tool_calls"] and msg["tool_calls_native"] is True

    adapter, _, _ = _guest(tmp_path, [_answer(response=TEXT_CALL)])
    msg, _ = asyncio.run(adapter.chat(TURNS, tools=PEER_TOOLS))
    assert msg["tool_calls"] and "tool_calls_native" not in msg


# --- the words in front of a written call reach a reader --------------------------------

REPORT = "Here is the full report. " * 200


def test_the_words_in_front_of_a_written_call_stand_in_the_answer_the_retry_wrote(tmp_path, monkeypatch):
    llm = _Script({"content": REPORT + "\n\n" + WELL_FORMED}, {"content": "Saved."})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == REPORT.strip() + "\n\nSaved."
    assert "kept and will be shown" in llm.requests[1]["messages"][-1]["content"]


def test_words_the_retry_repeats_are_not_shown_twice(tmp_path, monkeypatch):
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": "Short report.\n\nSaved."})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "Short report.\n\nSaved."


def test_when_the_retry_makes_the_call_the_words_go_with_that_rounds_text(tmp_path, monkeypatch):
    """The same reply as words plus a real call: its text is the round's, as for any tool round."""
    tools = _Tools()
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, _native(""), {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert text == ANSWER and tools.executed == ["search_in_file"]
    assert "Short report." in trace["accumulated_tool_calls"][0]["round_text"]


def test_the_note_carries_the_words_of_both_attempts(tmp_path, monkeypatch):
    llm = _Script({"content": "First words.\n\n" + WELL_FORMED}, {"content": "Second words.\n\n" + WELL_FORMED})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ Not done") and trace["turn_end"] == "guard"
    assert "First words." in text and "Second words." in text and "<tool_call" not in text


def test_the_words_survive_a_retry_that_comes_back_empty_to_the_end(tmp_path, monkeypatch):
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": ""})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ No answer") and text.endswith("\n\nShort report.") and trace["turn_end"] == "guard"


def test_the_words_survive_a_guard_that_stops_the_retry(tmp_path, monkeypatch):
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": ANSWER})
    text, _, _ = _loop(tmp_path, monkeypatch, llm, max_rounds=1)
    assert text == "Short report.\n\n" + ANSWER


def test_a_finalising_summary_in_front_of_a_written_call_survives_an_empty_ask_again():
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": ""})
    kept = []
    text, _, trace = asyncio.run(_finalize_after_guard_stop(
        _hooks(), [{"role": "user", "content": "q"}], llm, None, "c", {}, {},
        fallback_reason="stopped", tools=TOOLS, keep_unexecuted=lambda t: kept.append(t) or "addr",
    ))
    assert text == "Short report." and trace["guard_answered"] is True
    assert kept == ["Short report.\n\n" + WELL_FORMED]
    said, reason = llm.requests[1]["messages"][-2:]
    assert said == {"role": "assistant", "content": "Short report."}
    assert "`addr`" in reason["content"]


def test_a_finalising_summary_in_front_of_a_written_call_stands_above_the_answer():
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": "Saved."})
    text, _, _ = _after_guard(llm)
    assert text == "Short report.\n\nSaved."


# --- the body of a retried call is kept --------------------------------------------------

BODY = ("<tool_call>\n<function=write_file>\n<parameter=content>\n" + "the write-up " * 500
        + "\n</parameter>\n</function>\n</tool_call>")


def test_the_body_of_a_retried_call_is_kept_and_the_retry_points_at_it(tmp_path, monkeypatch):
    llm = _Script({"content": BODY}, {"content": "I could not do it."})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == "I could not do it."
    kept_at = trace["written_call_retries"][0]["kept_at"]
    assert (tmp_path / kept_at).read_bytes() == BODY.encode("utf-8")
    assert f"`{kept_at}`" in llm.requests[1]["messages"][-1]["content"]


def test_the_same_text_written_twice_is_kept_once(tmp_path, monkeypatch):
    llm = _Script({"content": BODY})
    _, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert trace["unexecuted_tool_calls"][0]["kept_at"] == trace["written_call_retries"][0]["kept_at"]
    assert len(list(tmp_path.rglob("unexecuted-*"))) == 1


def test_a_failed_keep_still_asks_again(tmp_path, monkeypatch):
    (tmp_path / "state").write_text("a file where the store's directory should be")
    llm = _Script({"content": BODY}, {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and trace["written_call_retries"][0]["kept_at"] is None
    assert "kept at" not in llm.requests[1]["messages"][-1]["content"]


# --- text behind a role marker -------------------------------------------------------------

@pytest.mark.parametrize("tail", ["\n[USER] asked about X, and here is the reply. " + LONG,
                                  "\n\n[TOOL RESULT: c] ok.\n\nSo the answer: " + LONG])
def test_a_block_then_a_role_marker_posts_no_markup_and_keeps_the_text(tmp_path, monkeypatch, tail):
    """The role cut (2026-09-03) takes everything from the marker on whichever way this rules, so
    reading the block as a quote would post the bare block. Read as a call the model is asked again."""
    content = WELL_FORMED + tail
    llm = _Script({"content": content}, {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER
    assert (tmp_path / trace["written_call_retries"][0]["kept_at"]).read_text("utf-8") == content


# --- the note is the runtime's, the person's turn stays the person's -------------------------

def _describing_adapter():
    return DpcLlmAdapter.__new__(DpcLlmAdapter)


IMAGE_TURN = {"role": "user", "content": [
    {"type": "text", "text": "what is on it?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}


@pytest.mark.parametrize("second", [{"content": ANSWER}])
def test_a_retry_leaves_the_image_description_on_the_persons_turn(tmp_path, monkeypatch, second):
    llm = _Script({"content": JOHNNY}, second)
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    asyncio.run(run_llm_loop(messages=[dict(IMAGE_TURN)], tools=_Tools(), llm=llm, agent_root=tmp_path,
                             emit_progress=lambda *a, **k: None, task_id="t"))
    sent = llm.requests[1]["messages"]
    adapter = _describing_adapter()
    described = adapter._inject_image_description_into_messages(sent, "a cat")
    assert "a cat" in described[0]["content"] and "what is on it?" in described[0]["content"]
    assert described[-1]["content"] == sent[-1]["content"], "the reason stays as it was"
    assert adapter._extract_user_text(sent) == "what is on it?"


def test_a_note_beside_a_call_leaves_the_image_description_on_the_persons_turn(tmp_path, monkeypatch):
    llm = _Script(_native(JOHNNY), {"content": ANSWER})
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    asyncio.run(run_llm_loop(messages=[dict(IMAGE_TURN)], tools=_Tools(), llm=llm, agent_root=tmp_path,
                             emit_progress=lambda *a, **k: None, task_id="t"))
    sent = llm.requests[1]["messages"]
    described = _describing_adapter()._inject_image_description_into_messages(sent, "a cat")
    assert "a cat" in described[0]["content"] and "a cat" not in str(described[-1]["content"])


def test_the_runtimes_mark_does_not_reach_the_wire_or_the_prompt_measure(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY}, {"content": ANSWER})
    _loop(tmp_path, monkeypatch, llm)
    sent = llm.requests[1]["messages"]
    _, turns = DpcLlmAdapter._convert_messages_to_anthropic(sent)
    assert "_runtime_note" not in json.dumps(turns)


# --- a written round is no progress for ResearchLimitGuard ------------------------------------

def _no_progress_call(n):
    """A tool the guard does not count as progress, with new arguments so LoopGuard stays out of it."""
    return _native("", {"id": f"c{n}", "type": "function",
                        "function": {"name": "t", "arguments": json.dumps({"n": n})}})


def test_a_written_round_does_not_reset_the_research_limit(tmp_path, monkeypatch):
    """15 tool-only rounds without progress stop a run; a written call every 15 used to carry it on."""
    answers = [_no_progress_call(n) for n in range(14)] + [{"content": WELL_FORMED}]
    answers += [_no_progress_call(n) for n in range(100, 130)]
    llm = _Script(*answers)
    _loop(tmp_path, monkeypatch, llm)
    stops = [i for i, r in enumerate(llm.requests)
             if any(str(m.get("content")).startswith("[RESEARCH_LIMIT]") for m in r["messages"])]
    # 14 calls, the written round, the retry's call, which is the 15th: the next request carries the stop.
    assert stops and stops[0] == 16


def test_words_in_front_of_a_written_call_still_count_as_text(tmp_path, monkeypatch):
    answers = [_no_progress_call(n) for n in range(14)] + [{"content": "A word.\n\n" + WELL_FORMED}]
    answers += [_no_progress_call(n) for n in range(100, 105)] + [{"content": ANSWER}]
    llm = _Script(*answers)
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and len(llm.requests) == 21


# --- the roles alternate ----------------------------------------------------------------------

def test_a_retry_in_the_first_round_keeps_the_roles_alternating(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY}, {"content": ANSWER})
    _loop(tmp_path, monkeypatch, llm)
    sent = llm.requests[1]["messages"]
    _, turns = DpcLlmAdapter._convert_messages_to_anthropic(sent)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert "<tool_call" not in json.dumps(turns)


def test_a_retry_after_tool_results_joins_their_turn(tmp_path, monkeypatch):
    llm = _Script(_native(""), {"content": JOHNNY}, {"content": ANSWER})
    _loop(tmp_path, monkeypatch, llm)
    sent = llm.requests[2]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "user"]
    _, turns = DpcLlmAdapter._convert_messages_to_anthropic(sent)
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]


# --- refused markup with text after it ---------------------------------------------------------

def test_refused_markup_leaves_the_replay_even_with_text_after_it(tmp_path, monkeypatch):
    llm = _Script(_native(WELL_FORMED + "\n\nChecking both.", rejected_tool_calls=["update_scratchpad"]),
                  {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER
    sent = llm.requests[1]["messages"]
    assert not any("<tool_call" in str(m.get("content")) for m in sent)
    assert sent[-3]["content"] == "Checking both."
    assert "<tool_call" not in trace["accumulated_tool_calls"][0]["round_text"]


def test_a_quote_beside_a_call_with_no_refusal_stays_in_the_text(tmp_path, monkeypatch):
    """Nobody refused it and text follows it: a sentence about the format, left alone."""
    llm = _Script(_native(WELL_FORMED + "\n\nThat is the form."), {"content": ANSWER})
    _, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert "written_calls_beside" not in trace
    assert llm.requests[1]["messages"][-2]["content"] == WELL_FORMED + "\n\nThat is the form."


# --- a fence with a sentence after it ------------------------------------------------------------

def test_a_fence_with_a_sentence_after_it_ends_in_a_note_and_not_in_nothing(tmp_path, monkeypatch):
    llm = _Script({"content": "```tool_call\nnot json\n```\n\nNow waiting."})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ Not done") and text.endswith("Now waiting.")
    assert trace["turn_end"] == "guard"


# --- a call that ran is not reported as never run ------------------------------------------

def test_a_note_after_a_guard_does_not_say_a_call_that_ran_mid_turn_was_never_executed(tmp_path, monkeypatch):
    """The note is replayed whole into the next prompt: "never executed" of a write that ran
    is what makes the model repeat it."""
    tools = _Tools()
    llm = _Script(_native(), {"content": JOHNNY})
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools, max_rounds=1)
    assert tools.executed == ["search_in_file"] and trace["turn_end"] == "guard"
    assert "never executed" not in text and "ask again to have it done" not in text
    assert text.startswith("⚠️ Not done") and "`search_in_file` did run earlier in this turn" in text


def test_a_note_after_a_guard_does_not_say_a_call_of_its_own_dispatch_round_was_never_executed(tmp_path, monkeypatch):
    tools = _Tools()
    llm = _Script(_native(), {"content": JOHNNY})
    text, _, _ = _loop(tmp_path, monkeypatch, llm, tools=tools, max_rounds=0)
    assert tools.executed == ["search_in_file"]
    assert "never executed" not in text and "`search_in_file` did run earlier in this turn" in text


def test_a_note_after_a_stop_does_not_say_a_call_that_ran_was_never_executed(tmp_path, monkeypatch):
    stop = asyncio.Event()

    class _StopsWhileItRuns(_Tools):
        def execute(self, name, args, ctx=None):
            stop.set()
            return super().execute(name, args, ctx)

    tools = _StopsWhileItRuns()
    llm = _Script(_native(), {"content": JOHNNY})
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools, stop_event=stop)
    assert tools.executed == ["search_in_file"] and trace["turn_end"] == "stopped"
    assert "never executed" not in text and "`search_in_file` did run earlier in this turn" in text


def test_a_note_at_the_end_of_a_round_does_not_say_a_call_that_ran_was_never_executed(tmp_path, monkeypatch):
    tools = _Tools()
    llm = _Script(_native(), {"content": JOHNNY})
    text, _, _ = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert "never executed" not in text and "`search_in_file` did run earlier in this turn" in text


def test_a_note_keeps_never_executed_for_the_tool_that_did_not_run():
    trace = {"accumulated_tool_calls": [{"tool": "search_in_file"}]}
    text = _answer_without_unrun_calls(JOHNNY + "\n" + WELL_FORMED, trace)
    assert "`update_scratchpad` was never executed" in text
    assert "`search_in_file` did run earlier in this turn" in text
    assert "`search_in_file` was never executed" not in text


def test_the_retrys_reason_warns_of_a_tool_that_already_ran(tmp_path, monkeypatch):
    llm = _Script(_native(), {"content": JOHNNY}, {"content": ANSWER})
    _loop(tmp_path, monkeypatch, llm)
    assert "`search_in_file` did run earlier in this turn" in llm.requests[2]["messages"][-1]["content"]


def test_the_retrys_reason_says_nothing_of_a_tool_that_did_not_run(tmp_path, monkeypatch):
    llm = _Script({"content": JOHNNY}, {"content": ANSWER})
    _loop(tmp_path, monkeypatch, llm)
    assert "earlier in this turn" not in llm.requests[1]["messages"][-1]["content"]


def test_the_note_beside_a_call_warns_of_the_twin_by_the_name_the_launcher_fused(tmp_path, monkeypatch):
    llm = _Script(_native(JOHNNY, rejected_tool_calls=["search_in_file=context_lines"]), {"content": ANSWER})
    _loop(tmp_path, monkeypatch, llm)
    told = llm.requests[1]["messages"][-1]["content"]
    assert "`search_in_file` did run earlier in this turn" in told


def test_a_finalising_ask_warns_of_a_tool_that_already_ran(tmp_path, monkeypatch):
    llm = _Script(_native(), {"content": JOHNNY}, {"content": ANSWER})
    text, _, _ = _loop(tmp_path, monkeypatch, llm, max_rounds=1)
    assert text == ANSWER
    assert "`search_in_file` did run earlier in this turn" in llm.requests[2]["messages"][-1]["content"]


# --- an invented result ----------------------------------------------------------------------

INVENTED = "\n<tool_response>\nC:\\Users\\someone\\secret.txt: invented\n</tool_response>"


def test_a_call_followed_by_an_invented_result_is_a_written_call(tmp_path, monkeypatch):
    llm = _Script({"content": WELL_FORMED + INVENTED}, {"content": ANSWER})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER and len(llm.requests) == 2
    assert "invented" not in str(llm.requests[1]["messages"])
    assert (tmp_path / trace["written_call_retries"][0]["kept_at"]).read_text("utf-8").endswith("</tool_response>")


def test_a_refused_call_followed_by_an_invented_result_is_decided_by_the_text_alike(tmp_path, monkeypatch):
    llm = _Script({"content": WELL_FORMED + INVENTED, "rejected_tool_calls": ["x"]}, {"content": ANSWER})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == ANSWER


def test_an_invented_result_after_words_reaches_no_reader(tmp_path, monkeypatch):
    llm = _Script({"content": "Let me look." + INVENTED})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "Let me look." and len(llm.requests) == 1


def test_an_invented_result_in_a_finalising_answer_reaches_no_reader():
    text, _, trace = _after_guard(_Script({"content": "Let me look." + INVENTED}))
    assert text == "Let me look." and trace["guard_answered"] is True


def test_an_invented_result_in_the_summary_after_a_stop_reaches_no_reader():
    text, _, _ = asyncio.run(_finalize_after_user_stop(
        [{"role": "user", "content": "q"}], _Script({"content": "Where I got to." + INVENTED}), None, "c", {}, {},
        "⚠️ Stopped by user after 3 rounds.", tools=TOOLS,
    ))
    assert text == "Where I got to."


@pytest.mark.parametrize("content", [
    "A result comes back in `<tool_response>` tags.",
    "The template form:\n\n```xml\n<tool_response>\nresult\n</tool_response>\n```\n\nThat is what it reads.",
    "Quoted, in a fence left open:\n\n```\n<tool_response>\nresult",
])
def test_prose_naming_the_tag_inside_code_stays(tmp_path, monkeypatch, content):
    llm = _Script({"content": content})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.strip() == content.strip()


def test_prose_naming_the_tag_in_mid_sentence_stays(tmp_path, monkeypatch):
    """Every stored mention is this team talking about the format, none an invented result."""
    content = "The model then invents a <tool_response> block itself, which is the bug."
    llm = _Script({"content": content})
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == content and len(llm.requests) == 1


def test_prose_opening_a_line_with_the_tag_loses_what_follows_and_the_log_says_so(tmp_path, monkeypatch, caplog):
    """The price, the same as `[TOOL RESULT:`'s: cut at the tag, by name in the log."""
    llm = _Script({"content": "A result looks like this:\n<tool_response> and then its body."})
    with caplog.at_level("WARNING"):
        text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "A result looks like this:"
    assert any("<tool_response>" in r.getMessage() for r in caplog.records)


def test_an_invented_result_glued_to_the_closer_still_makes_a_written_call():
    names, _ = _written_tool_calls(WELL_FORMED + "<tool_response>invented</tool_response>")
    assert names == ["update_scratchpad"]


# --- the check sees the retry's reply, not the words in front of it ----------------------------

LONE_JSON = '{"name": "t", "arguments": {"a": "b"}}'


def test_a_retry_that_answers_with_a_lone_call_is_a_note_under_the_words_kept(tmp_path, monkeypatch):
    """With the words put on first, the JSON was no longer lone: posted raw, marked `guard`, kept nowhere."""
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": LONE_JSON})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ Not done") and text.endswith("\n\nShort report.")
    assert LONE_JSON not in text and "`t` was never executed" in text
    assert (tmp_path / trace["unexecuted_tool_calls"][0]["kept_at"]).read_text("utf-8") == LONE_JSON


def test_a_finalising_ask_answered_with_a_lone_call_is_a_note_under_the_words_kept():
    llm = _Script({"content": "Short report.\n\n" + WELL_FORMED}, {"content": LONE_JSON})
    text, _, trace = _after_guard(llm)
    assert text.startswith("⚠️ Not done") and text.endswith("\n\nShort report.")
    assert LONE_JSON not in text and trace["turn_end"] == "guard"


# --- "it was told" only when it was told -----------------------------------------------------------

def test_a_written_call_past_the_cap_does_not_say_it_was_told(tmp_path, monkeypatch):
    answers = []
    for n in range(MAX_WRITTEN_CALL_RETRIES):
        answers += [{"content": JOHNNY}, _native("", _valid(f"v{n}"))]
    llm = _Script(*answers, {"content": JOHNNY})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(trace["written_call_retries"]) == MAX_WRITTEN_CALL_RETRIES
    assert text.startswith("⚠️ Not done") and "It was told" not in text


# --- the words reach the reader when the retry round raises --------------------------------------

class _RaisesOnTheRetry(_Script):
    def __init__(self, error):
        super().__init__({"content": "Short report.\n\n" + WELL_FORMED})
        self.error = error

    async def chat(self, messages, tools=None, **kwargs):
        if self.requests:
            raise self.error
        return await super().chat(messages, tools=tools, **kwargs)


@pytest.mark.parametrize("error,turn_end,opening", [
    (CallKilled(), "killed", "⚠️ Stopped by user: killed mid-call"),
    (PeerRefused("not today", code=REFUSAL_NOT_ALLOWED), "refused", None),
    (TakenOutOfTheCardQueue("out", "a", "strata"), "stopped", "⚠️ Stopped by user: taken out"),
], ids=["killed", "refused", "taken-out"])
def test_the_words_stand_above_the_notice_when_the_retry_ends_without_a_failure(
        tmp_path, monkeypatch, error, turn_end, opening):
    text, _, trace = _loop(tmp_path, monkeypatch, _RaisesOnTheRetry(error))
    assert text.startswith("Short report.\n\n") and trace["turn_end"] == turn_end
    if opening:
        assert opening in text


@pytest.mark.parametrize("error,opening", [
    (RuntimeError("boom"), "⚠️ LLM error: boom"),
    (CardQueueRefusal("the card is held", "a", "strata"), "⚠️ the card is held"),
], ids=["llm-error", "engine-refusal"])
def test_the_error_stays_the_first_line_of_a_failed_turn_and_the_words_go_under_it(
        tmp_path, monkeypatch, error, opening):
    """The replay of a `failed` record is its first line, so that line stays the error."""
    text, _, trace = _loop(tmp_path, monkeypatch, _RaisesOnTheRetry(error))
    assert trace["turn_end"] == "failed"
    assert text.startswith(opening) and text.endswith("\n\nShort report.")


def test_the_words_reach_a_loop_error_too(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})

    def _fails_on_the_retry_line(message, *a, **k):
        if "came back as text" in str(message):
            raise RuntimeError("no screen")

    text, _, trace = asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(),
        llm=_Script({"content": "Short report.\n\n" + WELL_FORMED}), agent_root=tmp_path,
        emit_progress=_fails_on_the_retry_line, task_id="t",
    ))
    assert trace["turn_end"] == "failed"
    assert text.startswith("⚠️ Loop error: no screen") and text.endswith("\n\nShort report.")


def test_the_words_reach_a_reply_the_provider_dropped(tmp_path, monkeypatch):
    class _Drops(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            answer, usage = await super().chat(messages, tools=tools, **kwargs)
            if len(self.requests) == 2:
                usage = {"prompt_tokens": 0, "completion_tokens": 0}
            return answer, usage

    llm = _Drops({"content": "Short report.\n\n" + WELL_FORMED}, {"content": ""})
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert trace["turn_end"] == "failed"
    assert "dropped the request" in text.splitlines()[0] and text.endswith("\n\nShort report.")


@pytest.mark.asyncio
async def test_a_group_reads_the_failed_notice_and_the_words_go_to_the_owner_only():
    """The limit of carrying them: a group's `failed` post is the notice alone."""
    from dpc_client_core.dpc_agent.context import FAILED_TURN_NOTICE
    from tests.test_a_group_turn_that_ended_without_an_answer_posts_a_notice_and_wakes_nobody import (
        GROUP,
        _posted,
        _Service,
        _toasts,
    )

    service = _Service()
    await service.send_group_agent_message(GROUP, "Ark", "⚠️ LLM error: boom\n\nShort report.", turn_end="failed")
    assert _posted(service)["text"] == FAILED_TURN_NOTICE
    assert "Short report." in _toasts(service)[0]["message"]
