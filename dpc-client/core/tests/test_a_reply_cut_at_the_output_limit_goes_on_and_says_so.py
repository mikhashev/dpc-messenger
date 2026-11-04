"""A reply cut at the output limit is not the agent's whole answer: the agent is told and goes on.

Probe of 2026-11-04 on the real loop: a round with `finish_reason` length, text and no call ended
the turn on text that stopped in mid-word, with no note and `turn_end` empty. A native call cut in
its arguments ran the tool on `{}`. Mike's call, 2026-11-04: show the agent plainly that it hit the
limit, as other tool messages do, and let it continue by itself.

The usage dicts are the real providers': the section at the end drives each provider class through
the real adapter and the real loop, and pins the shape the doubles above it mirror. A provider
whose usage says nothing keeps the old behaviour, and those are named there.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.dpc_agent.context import compact_tool_history
from dpc_client_core.dpc_agent.guards import CONTEXT_LIMIT_RATIO, ContextLimitGuard
from dpc_client_core.dpc_agent.llm_adapter import DpcLlmAdapter
from dpc_client_core.dpc_agent.loop import (
    MAX_OUTPUT_LIMIT_CONTINUES_PER_TURN,
    MAX_OUTPUT_LIMIT_CONTINUES_SINCE_A_TOOL,
    USER_STOP_SUMMARY_REQUEST,
    _joined_at_the_output_limit,
    _output_limit_of,
    _reached_the_window,
    run_llm_loop,
)
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.node_ledger import NodeLedger
from dpc_client_core.providers import StrataProvider
from dpc_client_core.providers.anthropic_provider import AnthropicProvider
from dpc_client_core.providers.base import tool_use_of
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider
from dpc_client_core.providers.gemini_provider import GeminiProvider
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS
from dpc_client_core.providers.neuraldeep_provider import NeuralDeepProvider
from dpc_client_core.providers.ollama_provider import OllamaProvider
from dpc_client_core.providers.openai_provider import OpenAICompatibleProvider
from dpc_client_core.providers.zai_provider import ZaiProvider
from tests.test_a_group_turn_that_ended_without_an_answer_posts_a_notice_and_wakes_nobody import (
    GROUP,
    _Manager,
    _posted,
    _Service,
    _settled,
)
from tests.test_a_guest_agent_sends_its_tools_natively_where_the_host_serves_them import _answer, _guest
from tests.test_a_turn_that_ends_with_no_text_says_so_and_a_tool_refills_its_retries import NO_ANSWER, _Tools, _tool
from tests.test_a_turn_that_ends_without_an_answer_is_saved_with_why import (
    CID,
    _FakeAgent,
    _manager,
    _monitor,
    _on_disk,
)
from tests.test_llamacpp_server_provider import _FakeSupervisor, _chat_resp
from tests.test_strata_supervisor import _install

CEILING = 16384
# The usage dict of a Strata or llama-server round, as `_record_usage` builds it: pinned below.
STOPPED = {
    "prompt_tokens": 100, "completion_tokens": 40, "reasoning_tokens": 0, "content_tokens": 40,
    "total_tokens": 140, "cache_read_input_tokens": 0, "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 100, "output_includes_thinking": "includes", "served_effort": None,
    "finish_reason": "stop", "max_tokens": CEILING,
}
LENGTH = {**STOPPED, "completion_tokens": CEILING, "content_tokens": CEILING, "total_tokens": CEILING + 100,
          "finish_reason": "length"}
LIMIT = f"the output limit of {CEILING} tokens"
WRITTEN_CUT = "Writing it.\n<tool_call>\n<function=t>\n<parameter=a>\nhalf of the fi"
WRITTEN_WHOLE = "<tool_call>\n<function=t>\n<parameter=a>\nb\n</parameter>\n</function>\n</tool_call>"


def _cut(text="", **msg):
    return {"content": text, **msg}, LENGTH


def _done(text="", **msg):
    return {"content": text, **msg}, STOPPED


def _called(n, usage=STOPPED):
    return _tool(n), usage


class _Script:
    """Answers in order, each with its own usage; the last repeats, so a loop with no bound fails."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    async def chat(self, messages, tools=None, **kwargs):
        self.requests.append([dict(m) for m in messages])
        assert len(self.requests) < 80, "the loop did not stop"
        answer, usage = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return dict(answer), dict(usage)


def _loop(tmp_path, monkeypatch, llm, tools=None, config=None, **kwargs):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: dict(config or {}))
    return asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "write the document"}], tools=tools or _Tools(), llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t", **kwargs,
    ))


# --- a text cut -----------------------------------------------------------------------------------

def test_the_model_is_asked_again_and_told_where_and_the_reader_gets_one_text(tmp_path, monkeypatch):
    llm = _Script(_cut("Chapter one ... and then the sen"), _done("tence ended."))
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)

    assert len(llm.requests) == 2 and usage["rounds"] == 2
    assert text.startswith("Chapter one ... and then the sentence ended.\n\n⚠️ Continued:")
    assert "turn_end" not in trace
    words, told = llm.requests[1][-2:]
    assert words == {"role": "assistant", "content": "Chapter one ... and then the sen"}
    assert told["role"] == "user" and told["_runtime_note"] is True
    assert f"cut at {LIMIT}" in told["content"]
    assert "perhaps in mid-word" in told["content"]
    assert "the reader will see it with what you write next, as one text" in told["content"]
    assert "Go on from exactly that point without repeating anything" in told["content"]
    assert "if nothing is left to say, reply with nothing" in told["content"]


def test_the_note_at_the_end_names_the_join_and_counts_the_continues(tmp_path, monkeypatch):
    llm = _Script(_cut("one "), _cut("two "), _done("three"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == (f"one two three\n\n⚠️ Continued: this reply reached {LIMIT} and the agent went on 2 times, "
                    "so text may be repeated or missing where it was joined.")
    assert "turn_end" not in trace

    text, _, _ = _loop(tmp_path, monkeypatch, _Script(_cut("one "), _done("two")))
    assert "the agent went on once, so" in text


def test_a_continue_is_seen_live_logged_and_in_the_trace(tmp_path, monkeypatch, caplog):
    progress = []
    llm = _Script(_cut("the sen"), _cut("", thinking="r"), _done("tence"))
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    with caplog.at_level("WARNING"):
        _, _, trace = asyncio.run(run_llm_loop(
            messages=[{"role": "user", "content": "q"}], tools=_Tools(), llm=llm, agent_root=tmp_path,
            emit_progress=lambda *a, **k: progress.append(a[0]), task_id="t",
        ))
    assert progress.count("The reply reached the output limit; the agent is told and goes on.") == 2
    assert [c["kind"] for c in trace["output_limit_continues"]] == ["text", "no text"]
    assert [c["round"] for c in trace["output_limit_continues"]] == [1, 2]
    assert any(f"Round 1 stopped at {LIMIT} (text); the agent is told, continue 1/" in r.getMessage()
               for r in caplog.records)


def test_spaces_at_the_cut_stay(tmp_path, monkeypatch):
    llm = _Script(_cut("and then the "), _done("sentence"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("and then the sentence\n\n")


def test_a_reply_that_stopped_by_itself_gets_no_note_and_no_second_round(tmp_path, monkeypatch):
    llm = _Script(_done("All of it."))
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == "All of it." and usage["rounds"] == 1 and "turn_end" not in trace


@pytest.mark.parametrize("word", ["stop", "tool_calls", "content_filter", "end_turn", "", None, 1])
def test_only_the_limit_is_read_as_the_limit(word):
    assert _output_limit_of({**STOPPED, "finish_reason": word}) is None


def test_no_usage_is_no_limit():
    assert _output_limit_of({}) is None
    assert _output_limit_of(None) is None


@pytest.mark.parametrize("word", ["length", "max_tokens", "MAX_TOKENS"])
def test_each_vendors_word_for_the_limit_counts(word):
    assert _output_limit_of({"finish_reason": word, "max_tokens": 8}) == "the output limit of 8 tokens"


def test_without_a_ceiling_the_count_is_named_as_a_count():
    assert _output_limit_of({"finish_reason": "length", "completion_tokens": 900}) == (
        "the output limit, after about 900 tokens")
    assert _output_limit_of({"finish_reason": "length"}) == "the output limit"


# --- the join --------------------------------------------------------------------------------------

def test_a_reply_that_wrote_it_all_again_is_kept_once(tmp_path, monkeypatch):
    llm = _Script(_cut("The first part of it."), _done("The first part of it. And the rest."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("The first part of it. And the rest.\n\n")
    assert text.count("The first part") == 1


DOC = "".join(f"Paragraph {i} has some words in it to fill space. " for i in range(400))


def test_all_of_it_written_again_behind_a_few_words_is_kept_once():
    before = "An opening long enough not to be a phrase."
    assert _joined_at_the_output_limit(before, f"Again: {before} And more.") == f"Again: {before} And more."


def test_a_short_piece_that_happens_to_stand_in_what_follows_is_not_lost():
    """A cut after a long reasoning can leave a few words. Dropping them is the failure that costs text."""
    assert _joined_at_the_output_limit("The", " cat sat. The dog sat.") == "The cat sat. The dog sat."


def test_a_tail_written_again_is_taken_once():
    tail = " thirty three characters of tail."
    assert len(tail) == 33
    assert _joined_at_the_output_limit("aaaaa" + tail, tail + " and more") == "aaaaa" + tail + " and more"


def test_a_tail_written_again_is_found_however_long_it_is():
    assert _joined_at_the_output_limit(DOC[:15000], DOC[9000:16000]) == DOC[:16000]


def test_a_short_phrase_shared_at_the_join_is_no_repeat():
    """Dropping a real repeat is worse than showing a short one twice, which the note covers."""
    assert _joined_at_the_output_limit("the book", "keeper") == "the bookkeeper"
    phrase = "    return None  # the same lin"
    assert len(phrase) == 31
    assert _joined_at_the_output_limit("x" * 40 + phrase, phrase + "!") == "x" * 40 + phrase + phrase + "!"


def test_a_short_reply_that_stands_at_the_end_of_the_text_is_new_text():
    assert _joined_at_the_output_limit("the end of the word", "word") == "the end of the wordword"


def test_the_tail_is_found_behind_an_earlier_place_with_the_same_phrase():
    phrase = "a phrase that stands in the text more than once"
    assert _joined_at_the_output_limit(f"{phrase} and the middle, {phrase}", f"{phrase} and more") == (
        f"{phrase} and the middle, {phrase} and more")


def test_a_phrase_found_earlier_that_does_not_run_to_the_end_is_no_repeat():
    shared = "the same long phrase of forty characters"
    before = f"{shared} and then the text goes elsewhere"
    assert _joined_at_the_output_limit(before, f"{shared} and a different ending") == (
        f"{before}{shared} and a different ending")


# --- a restart from the top

def test_a_restart_cut_again_keeps_one_copy():
    assert _joined_at_the_output_limit(DOC[:15000], DOC[:14000]) == DOC[:15000]


def test_a_restart_longer_than_what_stood_is_the_copy_kept():
    assert _joined_at_the_output_limit(DOC[:5000], DOC[:9000]) == DOC[:9000]


def test_a_restart_that_changes_its_words_later_on_keeps_the_longer_copy():
    again = DOC[:6000] + "Written differently from here on. " * 100
    assert _joined_at_the_output_limit(DOC[:15000], again[:8000]) == DOC[:15000]
    assert _joined_at_the_output_limit(DOC[:6500], again) == again


def test_the_same_opening_shorter_than_a_restart_is_a_real_continuation():
    opening = "Dear colleague, " * 12  # 192 characters
    assert len(opening) < 200
    before = opening + "the first letter. "
    assert _joined_at_the_output_limit(before, opening + "the second letter.") == (
        before + opening + "the second letter.")


def test_a_second_letter_with_a_long_opening_and_a_longer_body_is_a_real_continuation():
    """The opening is past 200 characters but under half of the shorter piece: nothing is dropped."""
    opening = "Dear colleague, " * 20
    first = opening + "the first letter goes on for a while. " * 30
    second = opening + "the second letter goes on for a while too. " * 30
    assert _joined_at_the_output_limit(first, second) == first + second


def test_a_restart_cut_again_and_then_finished_comes_out_as_one_document(tmp_path, monkeypatch):
    """The reviewer's run, 2026-11-05: 19 890 characters became 35 055."""
    llm = _Script(_cut(DOC[:15000]), _cut(DOC[:14000]), _done(DOC[14000:]))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    body = text.split("\n\n⚠️ Continued")[0]
    assert body == DOC.rstrip() and body.count("Paragraph 5 has") == 1


def test_the_note_on_a_join_does_not_promise_how_small_a_repeat_is(tmp_path, monkeypatch):
    text, _, _ = _loop(tmp_path, monkeypatch, _Script(_cut("one "), _done("two")))
    assert text.endswith("the agent went on once, so text may be repeated or missing where it was joined.")


def test_the_history_marker_in_front_of_what_follows_goes(tmp_path, monkeypatch):
    llm = _Script(_cut("[#7 | 10:00 | Ark]\nthe sen"), _done("[#8 | 10:01 | Ark]\ntence"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("the sentence\n\n")
    assert llm.requests[1][-2]["content"] == "the sen"


# --- the bound ---------------------------------------------------------------------------------------

def test_the_bound_since_a_tool_ends_with_the_text_and_the_note(tmp_path, monkeypatch):
    llm = _Script(_cut("one "), _cut("two "), _cut("three"), _done("never asked"))
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(llm.requests) == MAX_OUTPUT_LIMIT_CONTINUES_SINCE_A_TOOL + 1 == 3
    assert text == (f"one two three\n\n⚠️ Cut off: the model's reply stopped at {LIMIT} here, so the text above is "
                    "not the whole of it. It had gone on 2 times. Ask it to go on to get the rest.")
    assert trace["turn_end"] == "guard" and usage["rounds"] == 3


def test_a_tool_gives_the_continues_back_and_the_turn_still_has_its_limit(tmp_path, monkeypatch):
    answers = []
    for n in range(30):
        answers += [_cut("one "), _cut("two "), _called(n)]
    llm = _Script(*answers)
    tools = _Tools()
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    # Each pair of continues with the tool after it, until the turn's limit is spent.
    assert MAX_OUTPUT_LIMIT_CONTINUES_PER_TURN == 6
    assert len(tools.executed) == 3
    assert len(llm.requests) == 3 * len(tools.executed) + 1
    assert "⚠️ Cut off" in text and trace["turn_end"] == "guard"


def test_the_round_limit_stands_above_the_continues_and_the_words_stay(tmp_path, monkeypatch):
    llm = _Script(_cut("the sen"), _done("The stop's answer."))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, max_rounds=1)
    assert any(str(m.get("content")).startswith("[ROUND_LIMIT]") for m in llm.requests[-1])
    assert text == "the sen\n\nThe stop's answer." and trace.get("guard_answered") is True


# --- a tool after the cut ----------------------------------------------------------------------------

def test_a_tool_after_the_cut_takes_the_words_into_its_round_and_the_answer_is_what_follows(tmp_path, monkeypatch):
    llm = _Script(_cut("Partial survey of the fi"), _called(1), _done("The answer."))
    tools = _Tools()
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert text == "The answer." and "turn_end" not in trace and len(tools.executed) == 1
    assert "Partial survey of the fi" in trace["accumulated_tool_calls"][0]["round_text"]


# --- a written call cut --------------------------------------------------------------------------------

def test_a_cut_inside_a_written_call_says_it_was_cut(tmp_path, monkeypatch):
    llm = _Script(_cut(WRITTEN_CUT), _done("Done in words."))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    told = llm.requests[1][-1]["content"]
    assert f"cut at {LIMIT} inside `t`, written as text, so it was not executed" in told
    assert "It has to fit the limit this time: write less in one reply." in told
    assert "Your words before it are kept" in told and "state/tool_results" in told
    assert text == "Writing it.\n\nDone in words." and len(trace["written_call_retries"]) == 1


def test_what_follows_a_written_call_after_a_cut_is_a_paragraph_of_its_own(tmp_path, monkeypatch):
    llm = _Script(_cut("The cut text."), _done("Next.\n" + WRITTEN_WHOLE), _done("In words."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("The cut text.\n\nNext.\n\nIn words.")


def test_a_written_call_past_its_retries_is_seen_alone_and_not_joined_to_the_cut_words(tmp_path, monkeypatch):
    """Joined, its opener would stand in mid-line and read as prose, and be posted."""
    llm = _Script(_done(WRITTEN_WHOLE), _cut("the sen"), _done(WRITTEN_WHOLE))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ Not done") and "<tool_call>" not in text and trace["turn_end"] == "guard"
    assert "⚠️ Continued" not in text and text.endswith("the sen")


def test_a_written_call_not_cut_keeps_its_own_sentence(tmp_path, monkeypatch):
    llm = _Script(_done(WRITTEN_WHOLE), _done("Words."))
    _loop(tmp_path, monkeypatch, llm)
    told = llm.requests[1][-1]["content"]
    assert "did not read as a call of a declared tool" in told and "cut at" not in told


def test_a_written_call_cut_again_ends_on_the_note_that_names_the_cut(tmp_path, monkeypatch):
    llm = _Script(_cut(WRITTEN_CUT))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ Not done: the agent wrote its last action as text instead of running it, "
                           f"and its reply was cut at {LIMIT} inside it, so `t` was never executed.")
    assert trace["turn_end"] == "guard"
    assert len(llm.requests) == 2, "the written-call retries, not the continues, bound this one"


# --- reasoning only --------------------------------------------------------------------------------------

def test_a_cut_with_reasoning_only_is_told_and_not_sent_blind(tmp_path, monkeypatch):
    llm = _Script(_cut("", thinking="long private reasoning"), _done("The answer."))
    text, usage, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == "The answer." and "turn_end" not in trace
    told = llm.requests[1][-1]
    assert told["_runtime_note"] is True
    assert f"cut at {LIMIT} before any text: the whole of it went on reasoning" in told["content"]
    assert "Asked again the same way it would end the same way, so reason less" in told["content"]
    assert llm.requests[1][-2] == {"role": "assistant", "content": "(This reply was cut before any text.)"}
    assert "private" not in str(llm.requests[1][-2:])


def test_a_cut_with_only_markers_says_nobody_could_read_it(tmp_path, monkeypatch):
    llm = _Script(_cut("[#74 | 06:42:57 | Johnny]" * 3), _done("Words."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "Words."
    assert f"cut at {LIMIT} and held no text anyone could read" in llm.requests[1][-1]["content"]


def test_reasoning_spent_on_every_try_ends_on_the_no_answer_note_naming_the_cut(tmp_path, monkeypatch):
    llm = _Script(_cut("", thinking="the private reasoning"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(llm.requests) == MAX_OUTPUT_LIMIT_CONTINUES_SINCE_A_TOOL + 1
    assert text.startswith(NO_ANSWER) and trace["turn_end"] == "guard"
    assert f"Its reply was cut at {LIMIT} before any text, and telling it so did not help." in text
    assert "private" not in text


def test_a_round_that_is_only_empty_stays_a_blind_resend(tmp_path, monkeypatch):
    llm = _Script(_done("", thinking="reasoning"), _done("The answer."))
    _loop(tmp_path, monkeypatch, llm)
    assert llm.requests[1] == llm.requests[0]


def test_the_empty_rounds_counter_is_not_spent_by_a_cut(tmp_path, monkeypatch):
    empty = _done("", thinking="r")
    llm = _Script(_cut("", thinking="r"), _cut("", thinking="r"), empty, empty, empty, _done("The answer."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "The answer."


def test_an_empty_reply_after_the_continue_ends_with_what_there_is(tmp_path, monkeypatch):
    """It was told it may finish by writing nothing: three blind sends would only cost a card."""
    llm = _Script(_cut("The whole of it."), _done("", thinking="nothing is left"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert len(llm.requests) == 2 and trace["turn_end"] == "guard"
    assert text == (f"The whole of it.\n\n⚠️ Cut off: the model's reply stopped at {LIMIT} here, and told so it added "
                    "nothing, so this may or may not be the whole of it. Ask it to go on if something is missing.")


def test_an_answer_after_a_no_text_round_is_not_joined_into_the_words(tmp_path, monkeypatch):
    """Told to answer early, not to go on: fused, it read `Part one of the doc cuThe rest: part two.`"""
    llm = _Script(_cut("Part one of the doc cu"), _cut("", thinking="r"), _done("The rest: part two."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("Part one of the doc cu\n\nThe rest: part two.\n\n⚠️ Continued")


def test_a_no_text_round_says_the_words_before_it_stand(tmp_path, monkeypatch):
    llm = _Script(_cut("Part one of the doc cu"), _cut("", thinking="r"), _done("Rest."))
    _loop(tmp_path, monkeypatch, llm)
    in_place, told = llm.requests[2][-2:]
    assert in_place == {"role": "assistant", "content": "(This reply was cut before any text.)"}
    assert "The text you wrote before it is kept and will be shown; do not repeat it." in told["content"]
    assert "stands" not in in_place["content"]


def test_a_round_of_only_markers_says_the_words_before_it_stand_too(tmp_path, monkeypatch):
    llm = _Script(_cut("Part one."), _cut("[#74 | 06:42:57 | Johnny]"), _done("Rest."))
    _loop(tmp_path, monkeypatch, llm)
    assert "The text you wrote before it is kept" in llm.requests[2][-1]["content"]


def test_a_no_text_round_with_no_words_does_not_mention_them(tmp_path, monkeypatch):
    llm = _Script(_cut("", thinking="r"), _done("Rest."))
    _loop(tmp_path, monkeypatch, llm)
    assert "The text you wrote" not in llm.requests[1][-1]["content"]


def test_a_cut_with_no_text_after_a_cut_with_text_keeps_the_text_under_the_bound(tmp_path, monkeypatch):
    llm = _Script(_cut("The text so far."), _cut("", thinking="r"), _cut("", thinking="r"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("The text so far.\n\n⚠️ Cut off:") and trace["turn_end"] == "guard"


# --- a native call cut in its arguments ---------------------------------------------------------------------

def _cut_call(name="write_file", raw='{"path": "a.md", "content": "half of the fi'):
    return {"id": "c9", "type": "function", "function": {"name": name, "arguments": "{}"},
            "_unreadable_arguments": raw}


def _broken(usage=LENGTH, text="", name="write_file"):
    return {"content": text, "tool_calls": [_cut_call(name)], "tool_calls_native": True}, usage


def test_a_call_cut_in_its_arguments_does_not_run_and_the_agent_is_told(tmp_path, monkeypatch):
    llm = _Script(_broken(), _done("In words."))
    tools = _Tools()
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == [] and text == "In words." and "turn_end" not in trace
    told = llm.requests[1][-1]
    assert told["_runtime_note"] is True
    assert f"Your call of `write_file` was cut at {LIMIT}, inside its arguments, so it was not executed." in told["content"]
    assert "Make the call again with less in it, in smaller parts if it is long, or answer in words." in told["content"]
    assert "c9" not in str(llm.requests[1]), "no half call in the history"
    assert llm.requests[1][-2] == {"role": "assistant", "content": "(My tool call was not whole. It did not run.)"}


def test_what_the_cut_call_had_written_is_kept_and_its_address_given(tmp_path, monkeypatch):
    llm = _Script(_broken(), _done("In words."))
    _loop(tmp_path, monkeypatch, llm)
    told = llm.requests[1][-1]["content"]
    address = told.split("What it had written is kept at `")[1].split("`")[0]
    assert "half of the fi" in (tmp_path / address).read_text(encoding="utf-8")


def test_arguments_that_do_not_parse_with_no_signal_do_not_run_either(tmp_path, monkeypatch):
    silent = {k: v for k, v in LENGTH.items() if k != "finish_reason"}
    llm = _Script(_broken(usage=silent), _done("In words."))
    tools = _Tools()
    _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == []
    assert ("Your call of `write_file` came back with arguments that do not parse as JSON, so it was not executed."
            in llm.requests[1][-1]["content"])


def test_unreadable_arguments_with_no_signal_do_not_say_the_limit_on_the_live_row(tmp_path, monkeypatch):
    silent = {k: v for k, v in LENGTH.items() if k != "finish_reason"}
    progress = []
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    asyncio.run(run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(), llm=_Script(_broken(usage=silent), _done("Words.")),
        agent_root=tmp_path, emit_progress=lambda *a, **k: progress.append(a[0]), task_id="t",
    ))
    assert "A tool call came back unreadable and did not run; asking again." in progress
    assert not any("output limit" in str(p) for p in progress)


def test_words_beside_a_cut_call_stay(tmp_path, monkeypatch):
    llm = _Script(_broken(text="Saving it now."), _done("Saved nothing."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text == "Saving it now.\n\nSaved nothing."
    assert llm.requests[1][-2]["content"] == "Saving it now."


def test_what_follows_a_cut_call_is_a_paragraph_of_its_own(tmp_path, monkeypatch):
    llm = _Script(_cut("The cut text."), _broken(text="Saving it."), _done("Nothing saved."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("The cut text.\n\nSaving it.\n\nNothing saved.")


def test_the_words_of_every_try_stand_under_the_note_of_a_call_cut_each_time(tmp_path, monkeypatch):
    llm = _Script(_broken(text="First."), _broken(text="Second."), _broken(text="Third."))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("⚠️ Not done") and text.endswith("\n\nFirst.\n\nSecond.\n\nThird.")


def test_a_whole_call_beside_a_cut_one_runs_and_the_agent_is_told_after_it(tmp_path, monkeypatch):
    whole = _tool(1)["tool_calls"][0]
    msg = {"content": "", "tool_calls": [whole, _cut_call()], "tool_calls_native": True}
    llm = _Script((msg, LENGTH), _done("Done."))
    tools = _Tools()
    text, _, _ = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == [{"n": 1}] and text == "Done."
    assert "write_file" not in json.dumps(llm.requests[1][-3]["tool_calls"])
    assert llm.requests[1][-2]["role"] == "tool"
    assert "Your call of `write_file` was cut" in llm.requests[1][-1]["content"]
    assert llm.requests[1][-1]["_runtime_note"] is True


def test_a_host_gives_the_guest_no_marker_so_an_empty_last_call_in_a_cut_round_does_not_run(tmp_path, monkeypatch):
    """The wire carries `input` {} for arguments that did not parse."""
    msg = {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                          "function": {"name": "write_file", "arguments": "{}"}}],
           "tool_calls_native": True}
    llm = _Script((msg, LENGTH), _done("In words."))
    tools = _Tools()
    _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == []
    told = llm.requests[1][-1]["content"]
    assert "was cut at" in told and "kept at" not in told, "nothing came back to keep"


def test_an_empty_call_in_a_round_that_was_not_cut_runs(tmp_path, monkeypatch):
    msg = {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                          "function": {"name": "t", "arguments": "{}"}}],
           "tool_calls_native": True}
    llm = _Script((msg, STOPPED), _done("Done."))
    tools = _Tools()
    _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == [{}]


def test_an_empty_call_read_out_of_the_text_was_whole_because_the_text_held_all_of_it(tmp_path, monkeypatch):
    msg = {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                          "function": {"name": "t", "arguments": "{}"}}]}
    llm = _Script((msg, LENGTH), _done("Done."))
    tools = _Tools()
    _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == [{}]


def test_an_empty_call_before_the_last_in_a_cut_round_was_whole(tmp_path, monkeypatch):
    empty = {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    msg = {"content": "", "tool_calls": [empty, _tool(2)["tool_calls"][0]], "tool_calls_native": True}
    llm = _Script((msg, LENGTH), _done("Done."))
    tools = _Tools()
    _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == [{}, {"n": 2}]


def test_a_call_cut_on_every_try_ends_on_a_note_naming_it(tmp_path, monkeypatch):
    llm = _Script(_broken())
    tools = _Tools()
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools)
    assert tools.executed == [] and len(llm.requests) == MAX_OUTPUT_LIMIT_CONTINUES_SINCE_A_TOOL + 1
    assert text.startswith(f"⚠️ Not done: the agent's last action was cut at {LIMIT} inside its arguments, "
                           "so `write_file` was never executed.")
    assert "It was told and it happened again, so the turn ended here without its answer." in text
    assert "kept at `state/tool_results/" in text
    assert trace["turn_end"] == "guard"
    assert trace["unexecuted_tool_calls"][0]["tools"] == ["write_file"]


def test_unreadable_arguments_with_no_signal_end_on_a_note_saying_that(tmp_path, monkeypatch):
    silent = {k: v for k, v in LENGTH.items() if k != "finish_reason"}
    text, _, trace = _loop(tmp_path, monkeypatch, _Script(_broken(usage=silent)))
    assert text.startswith("⚠️ Not done: the arguments of the agent's last action came back unreadable, so "
                           "`write_file` was never executed.")
    assert trace["turn_end"] == "guard"


def test_a_tool_of_that_name_that_ran_earlier_is_not_called_never_executed(tmp_path, monkeypatch):
    llm = _Script(_called(1), _broken(name="t"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert "`t` did run earlier in this turn" in text and "`t` was never executed" not in text
    assert "`t` did run earlier in this turn" in llm.requests[2][-1]["content"]


# --- scaffolding -----------------------------------------------------------------------------------------------

def test_a_cut_inside_invented_scaffolding_is_no_cut_in_the_answer(tmp_path, monkeypatch):
    llm = _Script(_cut("The answer.\n[USER]\nand what I invented goes on"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm)
    assert text == "The answer." and len(llm.requests) == 1 and "turn_end" not in trace


def test_scaffolding_in_what_follows_is_cut_after_the_join(tmp_path, monkeypatch):
    llm = _Script(_cut("The sen"), _done("tence.\n[USER]\ninvented"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm)
    assert text.startswith("The sentence.\n\n⚠️ Continued:") and "invented" not in text


# --- the finalising calls ----------------------------------------------------------------------------------------

def test_a_cut_finalising_answer_says_so_and_is_not_continued(tmp_path, monkeypatch):
    llm = _Script(_cut("The summary stops in the mid"))
    text, usage, trace = _loop(tmp_path, monkeypatch, llm, max_rounds=0)
    assert len(llm.requests) == 1 and usage.get("rounds", 0) == 0
    assert text == (f"The summary stops in the mid\n\n⚠️ Cut off: the model's reply stopped at {LIMIT} here, so the "
                    "text above is not the whole of it. Ask it to go on to get the rest.")
    assert trace["turn_end"] == "guard" and trace["guard_answered"] is True


def test_a_finalising_call_cut_and_then_written_ends_on_the_note(tmp_path, monkeypatch):
    llm = _Script(_broken(), _done(WRITTEN_WHOLE))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, max_rounds=0)
    assert text.startswith("⚠️ Not done") and trace["turn_end"] == "guard"
    assert "It was told and wrote it as text again" in text


def test_a_finalising_answer_not_cut_stays_ordinary(tmp_path, monkeypatch):
    text, _, trace = _loop(tmp_path, monkeypatch, _Script(_done("The summary.")), max_rounds=0)
    assert text == "The summary." and "turn_end" not in trace


def test_a_finalising_call_cut_in_its_arguments_never_runs_and_is_told(tmp_path, monkeypatch):
    llm = _Script(_broken(), _done("In words."))
    tools = _Tools()
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools, max_rounds=0)
    assert tools.executed == [] and text == "In words." and trace.get("guard_answered") is True
    told = llm.requests[1][-1]["content"]
    assert f"Your call of `write_file` was cut at {LIMIT}" in told
    assert "No tool will run now: answer in words, with what you have." in told
    assert llm.requests[1][-2]["content"] == "(My tool call was not whole. It did not run.)"


def test_a_finalising_round_with_a_whole_call_beside_a_cut_one_tells_before_the_answer(tmp_path, monkeypatch):
    whole = _tool(1)["tool_calls"][0]
    msg = {"content": "", "tool_calls": [whole, _cut_call()], "tool_calls_native": True}
    llm = _Script((msg, LENGTH), _done("Done."))
    tools = _Tools()
    text, _, _ = _loop(tmp_path, monkeypatch, llm, tools=tools, max_rounds=0)
    assert tools.executed == [{"n": 1}] and text == "Done."
    told = llm.requests[1][-1]
    assert "Your call of `write_file` was cut" in told["content"] and told["_runtime_note"] is True
    assert "No tool will run now" in told["content"]


def test_a_finalising_written_call_cut_says_it_was_cut(tmp_path, monkeypatch):
    llm = _Script(_cut(WRITTEN_CUT), _done("In words."))
    _loop(tmp_path, monkeypatch, llm, max_rounds=0)
    assert f"cut at {LIMIT} inside `t`" in llm.requests[1][-1]["content"]


def test_a_cut_summary_after_a_stop_says_so(tmp_path, monkeypatch):
    stop = asyncio.Event()

    class _StopsAfterTheFirst(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            result = await super().chat(messages, tools=tools, **kwargs)
            stop.set()
            return result

    llm = _StopsAfterTheFirst(_called(1), _cut("Where I stop"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, stop_event=stop)
    assert text.startswith("Where I stop\n\n⚠️ Cut off:") and trace["turn_end"] == "stopped"


# --- the person's Stop and Kill ----------------------------------------------------------------------------------

def test_a_stop_after_the_cut_keeps_the_words_above_the_summary(tmp_path, monkeypatch):
    stop = asyncio.Event()

    class _StopsAfterTheCut(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            result = await super().chat(messages, tools=tools, **kwargs)
            stop.set()
            return result

    llm = _StopsAfterTheCut(_cut("the sen"), _done("Where I stopped."))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, stop_event=stop)
    assert text == "the sen\n\nWhere I stopped." and trace["turn_end"] == "stopped"
    assert llm.requests[-1][-1]["content"] == USER_STOP_SUMMARY_REQUEST


def test_a_kill_during_the_continue_keeps_the_words(tmp_path, monkeypatch):
    kill = asyncio.Event()

    class _KilledOnTheContinue(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            if self.requests:
                kill.set()
                await asyncio.Event().wait()
            return await super().chat(messages, tools=tools, **kwargs)

    text, _, trace = _loop(tmp_path, monkeypatch, _KilledOnTheContinue(_cut("the sen")), kill_event=kill)
    assert trace["turn_end"] == "killed" and text.startswith("the sen\n\n⚠️ Stopped by user")


# --- the window ---------------------------------------------------------------------------------------------------

def test_a_guard_at_the_window_stops_before_the_round_that_would_not_fit(tmp_path, monkeypatch):
    """The guard reads the last prompt, which the words just generated are not in yet."""
    near = {**LENGTH, "prompt_tokens": 14000, "completion_tokens": 5500, "content_tokens": 5500, "total_tokens": 19500,
            "max_tokens": 5500}
    llm = _Script(({"content": "the sen"}, near), _done("never asked"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, config={"context_window": 20000})
    assert len(llm.requests) == 1, "14000 alone is 70%; with what was just generated, 97.5%"
    assert text.startswith("the sen\n\n[CONTEXT_LIMIT]") and trace["turn_end"] == "guard"


def test_reasoning_is_not_counted_as_words_that_will_come_back(tmp_path, monkeypatch):
    """The reasoning of a turn with no call is not replayed."""
    near = {**LENGTH, "prompt_tokens": 14000, "completion_tokens": 5500, "reasoning_tokens": 5400,
            "content_tokens": 100, "total_tokens": 19500, "max_tokens": 5500}
    llm = _Script(({"content": "the sen"}, near), _done("tence"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm, config={"context_window": 20000})
    assert len(llm.requests) == 2 and text.startswith("the sentence")


# `length` from a window that is full: llama-server's slot stop, Ollama (per the reviewer, unread), and
# Strata's `fit_max_tokens`. Under the ceiling, at the window.
FULL = {**LENGTH, "prompt_tokens": 18000, "completion_tokens": 1990, "content_tokens": 1990, "total_tokens": 19990}
WINDOW = {"context_window": 20000}
AT_WINDOW = "the context window of 20000 tokens"


def test_the_two_limits_are_told_apart_by_the_numbers():
    assert _output_limit_of(FULL, 20000) == AT_WINDOW
    assert _reached_the_window(FULL, 20000) is True
    assert _reached_the_window(FULL, None) is False
    assert _reached_the_window(FULL, 0) is False
    at_ceiling = {**FULL, "completion_tokens": CEILING, "prompt_tokens": 18000}
    assert _reached_the_window(at_ceiling, 20000) is False, "the ceiling was reached: its own guard sees the rest"
    assert _output_limit_of(at_ceiling, 20000) == LIMIT
    assert _reached_the_window({**FULL, "finish_reason": "stop"}, 20000) is False


def test_no_usage_and_counts_that_are_not_numbers_say_nothing_about_the_window():
    assert _reached_the_window(None, 20000) is False
    from_a_wire = {"finish_reason": "length", "prompt_tokens": "lots", "completion_tokens": None}
    assert _reached_the_window(from_a_wire, 20000) is False
    assert _output_limit_of(from_a_wire, 20000) == "the output limit"


def test_a_stop_at_a_full_window_right_after_the_cut_names_the_window_in_the_summary(tmp_path, monkeypatch):
    stop = asyncio.Event()

    class _StopsAfterTheCut(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            result = await super().chat(messages, tools=tools, **kwargs)
            stop.set()
            return result

    llm = _StopsAfterTheCut(_cut("the sen"), ({"content": "Where I stop"}, FULL))
    text, _, _ = _loop(tmp_path, monkeypatch, llm, stop_event=stop, config=WINDOW)
    assert f"stopped at {AT_WINDOW} here" in text and "There is no room left to go on" in text


def test_the_line_is_the_guards_own():
    assert _reached_the_window({**FULL, "prompt_tokens": 17000, "completion_tokens": 1999}, 20000) is False
    assert _reached_the_window({**FULL, "prompt_tokens": 17000, "completion_tokens": 2000}, 20000) is True
    assert CONTEXT_LIMIT_RATIO == ContextLimitGuard()._ratio == 0.95


def test_with_no_ceiling_in_the_usage_the_window_alone_decides():
    no_ceiling = {k: v for k, v in FULL.items() if k != "max_tokens"}
    assert _output_limit_of(no_ceiling, 20000) == AT_WINDOW
    assert _output_limit_of(no_ceiling, 90000) == "the output limit, after about 1990 tokens"


def test_a_stop_short_of_the_ceiling_with_room_left_is_not_called_the_ceiling():
    """Strata's #606 (one token repeated 256 times) and #879 (non-finite logits) end a reply as `length`."""
    short = {**LENGTH, "completion_tokens": 256}
    assert _output_limit_of(short, 200000) == f"a stop of the engine's, after 256 of the {CEILING} tokens it may write"


def test_a_window_that_is_full_is_not_continued_and_the_note_names_it(tmp_path, monkeypatch):
    llm = _Script(({"content": "the sen"}, FULL), _done("never asked"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, config=WINDOW)
    assert len(llm.requests) == 1 and trace["turn_end"] == "guard" and "output limit" not in text
    assert text == (f"the sen\n\n⚠️ Cut off: the model's reply stopped at {AT_WINDOW} here, so the text above is not the "
                    "whole of it. There is no room left to go on: start a new task, or raise the agent's "
                    "context_window if the model has room.")


def test_a_stop_short_of_the_ceiling_with_room_left_goes_on(tmp_path, monkeypatch):
    short = {**LENGTH, "completion_tokens": 256, "content_tokens": 256}
    llm = _Script(({"content": "the sen"}, short), _done("tence"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm, config=WINDOW)
    assert text.startswith("the sentence\n\n⚠️ Continued: this reply reached a stop of the engine's")
    assert "a stop of the engine's, after 256 of" in llm.requests[1][-1]["content"]


def test_a_call_cut_at_a_full_window_is_not_asked_for_again(tmp_path, monkeypatch):
    llm = _Script(_broken(usage=FULL), _done("never asked"))
    tools = _Tools()
    text, _, trace = _loop(tmp_path, monkeypatch, llm, tools=tools, config=WINDOW)
    assert len(llm.requests) == 1 and tools.executed == [] and trace["turn_end"] == "guard"
    assert text.startswith(f"⚠️ Not done: the agent's last action was cut at {AT_WINDOW} inside its arguments")
    assert "It was told" not in text and "There is no room left to go on" in text


def test_a_written_call_cut_at_a_full_window_is_not_asked_for_again(tmp_path, monkeypatch):
    llm = _Script(({"content": WRITTEN_CUT}, FULL), _done("never asked"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, config=WINDOW)
    assert len(llm.requests) == 1 and trace["turn_end"] == "guard"
    assert f"its reply was cut at {AT_WINDOW} inside it" in text and "It was told" not in text
    assert "There is no room left to go on" in text


def test_reasoning_that_filled_the_window_is_not_told_to_try_again(tmp_path, monkeypatch):
    llm = _Script(({"content": "", "thinking": "r"}, FULL), _done("never asked"))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, config=WINDOW)
    assert len(llm.requests) == 1 and trace["turn_end"] == "guard" and text.startswith(NO_ANSWER)
    assert f"Its reply was cut at {AT_WINDOW} before any text." in text
    assert "telling it so" not in text and "There is no room left to go on" in text
    assert "Ask again" not in text


def test_words_kept_from_an_earlier_cut_stand_above_a_full_window(tmp_path, monkeypatch):
    llm = _Script(_cut("The text so far."), ({"content": "", "thinking": "r"}, FULL))
    text, _, _ = _loop(tmp_path, monkeypatch, llm, config=WINDOW)
    assert text.startswith(f"The text so far.\n\n⚠️ Cut off: the model's reply stopped at {AT_WINDOW} here")
    assert "It had gone on once." in text and "There is no room left to go on" in text


def test_a_finalising_answer_at_a_full_window_names_the_window(tmp_path, monkeypatch):
    text, _, trace = _loop(tmp_path, monkeypatch, _Script(({"content": "The summ"}, FULL)), config=WINDOW, max_rounds=0)
    assert text.startswith(f"The summ\n\n⚠️ Cut off: the model's reply stopped at {AT_WINDOW} here")
    assert "There is no room left to go on" in text and trace["turn_end"] == "guard"


def test_a_guard_after_the_call_that_stops_at_a_full_window_names_the_window(tmp_path, monkeypatch):
    """ToolLimitGuard fires on the round's calls, before they run."""
    burst = {"content": "", "tool_calls_native": True, "tool_calls": [_tool(n)["tool_calls"][0] for n in range(26)]}
    llm = _Script((burst, STOPPED), ({"content": "The summ"}, FULL))
    tools = _Tools()
    text, _, _ = _loop(tmp_path, monkeypatch, llm, tools=tools, config=WINDOW)
    assert tools.executed == [] and any("[TOOL_LIMIT]" in str(m.get("content")) for m in llm.requests[-1])
    assert f"stopped at {AT_WINDOW} here" in text


def test_a_summary_after_a_stop_at_a_full_window_names_the_window(tmp_path, monkeypatch):
    stop = asyncio.Event()

    class _StopsAfterTheFirst(_Script):
        async def chat(self, messages, tools=None, **kwargs):
            result = await super().chat(messages, tools=tools, **kwargs)
            stop.set()
            return result

    llm = _StopsAfterTheFirst(_called(1), ({"content": "Where I stop"}, FULL))
    text, _, trace = _loop(tmp_path, monkeypatch, llm, stop_event=stop, config=WINDOW)
    assert text.startswith(f"Where I stop\n\n⚠️ Cut off: the model's reply stopped at {AT_WINDOW}")
    assert trace["turn_end"] == "stopped" and "There is no room left to go on" in text


def test_the_context_cut_is_told_the_same_size_as_the_guard(tmp_path, monkeypatch):
    seen = []

    async def _compaction(messages, *, last_prompt_tokens, **_kwargs):
        seen.append(last_prompt_tokens)
        return messages

    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.apply_compaction", _compaction)
    _loop(tmp_path, monkeypatch, _Script(_cut("the sen"), _called(1), _done("Done.")))
    # Once: the next round's own prompt holds the words.
    assert seen == [0, STOPPED["prompt_tokens"] + CEILING, STOPPED["prompt_tokens"]]


def test_a_context_cut_leaves_the_cut_words_and_the_note_alone():
    messages = []
    for n in range(4):
        messages += [{"role": "assistant", "content": "x" * 300, "tool_calls": [
            {"id": f"c{n}", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"c{n}", "content": "y"}]
    words = {"role": "assistant", "content": "z" * 5000}
    told = {"role": "user", "content": "Your last message was cut", "_runtime_note": True}
    assert compact_tool_history(messages + [words, told], keep_recent=1)[-2:] == [words, told]


# --- what a stream shows, and the records ---------------------------------------------------------------------------

def test_a_stream_shows_one_piece_and_the_answer_is_the_same_text(tmp_path, monkeypatch):
    shown = []

    async def _on_chunk(chunk, _conversation):
        shown.append(chunk)

    class _Streams(_Script):
        async def chat(self, messages, tools=None, on_stream_chunk=None, conversation_id=None, **kwargs):
            answer, usage = await super().chat(messages, tools=tools, **kwargs)
            await on_stream_chunk(answer["content"], conversation_id)
            return answer, usage

    llm = _Streams(_cut("the sen"), _done("tence"))
    text, _, _ = _loop(tmp_path, monkeypatch, llm, on_stream_chunk=_on_chunk, conversation_id="c")
    assert "".join(shown) == "the sentence" and text.startswith("the sentence\n\n")


@pytest.mark.asyncio
async def test_the_chat_record_of_a_cut_turn_is_marked_and_stays_out_of_knowledge(tmp_path, monkeypatch, signing_identity):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    text, _, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(),
        llm=_Script(_cut("one "), _cut("two "), _cut("three")),
        agent_root=tmp_path / "agent", emit_progress=lambda *a, **k: None, task_id="t",
    )
    monitor = _monitor(tmp_path)
    await _manager(_FakeAgent(text, trace), monitor)._process_message_guarded("hello", CID)
    _, record = _on_disk(tmp_path)
    assert record["content"] == text and record["turn_end"] == "guard" and "one two three" in text
    assert [m.text for m in monitor.full_conversation] == ["hello"]


@pytest.mark.asyncio
async def test_a_group_reads_a_cut_turn_and_it_wakes_nobody(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    text, _, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(),
        llm=_Script(_cut("Over to @Muse "), _cut("now "), _cut("and")),
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t",
    )
    service = _Service(_Manager(text, trace))
    await service._invoke_agent_in_group(GROUP, "hello", "Mike", "agent_001")
    await _settled()
    assert _posted(service)["text"] == text and "⚠️ Cut off" in text and service.woken == []


@pytest.mark.asyncio
async def test_a_group_reads_a_continued_answer_as_an_answer(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    text, _, trace = await run_llm_loop(
        messages=[{"role": "user", "content": "q"}], tools=_Tools(),
        llm=_Script(_cut("The sen"), _done("tence.")),
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t",
    )
    service = _Service(_Manager(text, trace))
    await service._invoke_agent_in_group(GROUP, "hello", "Mike", "agent_001")
    await _settled()
    assert _posted(service)["text"] == text and "turn_end" not in trace


# --- each real provider, through the real adapter ---------------------------------------------------------------------

@pytest.fixture
def _engine_home(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "DPC_HOME", tmp_path / "dpc-home")
    _ACTIVE_SUPERVISORS.clear()
    yield
    _ACTIVE_SUPERVISORS.clear()


def _adapter(provider, tmp_path, alias="under_test"):
    manager = NS(token_count_manager=None, providers={alias: provider}, agent_provider=None, default_provider=alias)
    return DpcLlmAdapter(manager, provider_alias=alias, caller="agent_test", ledger=NodeLedger(tmp_path / "ledger"))


async def _through_the_loop(llm, tmp_path, tools=None, on_stream_chunk=None):
    return await run_llm_loop(
        messages=[{"role": "user", "content": "write the document"}], tools=tools or _Tools(), llm=llm,
        agent_root=tmp_path, emit_progress=lambda *a, **k: None, task_id="t",
        on_stream_chunk=on_stream_chunk, conversation_id="c",
    )


class _Completions:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    async def create(self, **params):
        self.bodies.append(params)
        return self.responses.pop(0)


def _openai_usage(completion=8192):
    return NS(prompt_tokens=100, completion_tokens=completion, total_tokens=100 + completion,
              completion_tokens_details=None)


def _strata(tmp_path, *responses):
    provider = StrataProvider("flash", {
        "type": "strata", "alias": "flash", "model": "qwen3.8-flash-next-iq3_xxs",
        "strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, "context_window": 200000, "max_tokens": CEILING,
    })
    provider.supervisor = _FakeSupervisor()
    completions = _Completions(*responses)
    client = NS(chat=NS(completions=completions))

    async def _ensure():
        return client

    provider._ensure = _ensure
    return provider, completions


@pytest.mark.asyncio
async def test_strata_tools_path_says_length_and_the_doubles_above_mirror_its_usage(tmp_path, monkeypatch, _engine_home):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    provider, completions = _strata(
        tmp_path,
        _chat_resp("Chapter one and then the sen", usage=_openai_usage(CEILING), finish_reason="length"),
        _chat_resp("tence.", usage=_openai_usage(5), finish_reason="stop"),
    )
    llm = _adapter(provider, tmp_path, "flash")
    text, _, trace = await _through_the_loop(llm, tmp_path)

    assert len(completions.bodies) == 2 and "turn_end" not in trace
    assert text.startswith("Chapter one and then the sentence.\n\n⚠️ Continued")
    assert f"cut at {LIMIT}" in str(completions.bodies[1]["messages"][-1]["content"])
    # The double's keys are what this provider really hands the loop.
    real = provider._last_usage
    assert set(LENGTH) <= set(real) and real["finish_reason"] == "stop" and real["max_tokens"] == CEILING


@pytest.mark.asyncio
async def test_strata_text_path_stream_says_length(tmp_path, monkeypatch, _engine_home):
    """No tools sent: the stream path, whose usage chunk comes after the reason."""
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})

    async def _stream(text, reason):
        yield NS(usage=None, choices=[NS(finish_reason=None, delta=NS(content=text, reasoning_content=None, tool_calls=None))])
        yield NS(usage=None, choices=[NS(finish_reason=reason, delta=NS(content=None, reasoning_content=None, tool_calls=None))])
        yield NS(usage=_openai_usage(CEILING), choices=[], model_extra={})

    provider, completions = _strata(tmp_path, _stream("the sen", "length"), _stream("tence", "stop"))
    tools = _Tools()
    tools.schemas = lambda **_k: []
    shown = []

    async def _on_chunk(chunk, _conversation):
        shown.append(chunk)

    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path, "flash"), tmp_path, tools=tools, on_stream_chunk=_on_chunk)
    assert "".join(shown) == "the sentence" and text.startswith("the sentence\n\n⚠️ Continued")
    assert LIMIT in completions.bodies[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_strata_marks_arguments_cut_at_the_limit(tmp_path, monkeypatch, _engine_home):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    call = NS(id="c1", function=NS(name="write_file", arguments='{"path": "a.md", "content": "half'))
    provider, completions = _strata(
        tmp_path,
        _chat_resp("", tool_calls=[call], usage=_openai_usage(CEILING), finish_reason="length"),
        _chat_resp("In words.", usage=_openai_usage(5), finish_reason="stop"),
    )
    tools = _Tools()
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path, "flash"), tmp_path, tools=tools)
    assert tools.executed == [] and text == "In words."
    assert f"`write_file` was cut at {LIMIT}" in str(completions.bodies[1]["messages"][-1]["content"])


class _OllamaMsg(dict):
    """Both access shapes, as the real response has them."""

    def __init__(self, content, thinking=None, tool_calls=None):
        super().__init__(content=content)
        self.content, self.thinking, self.tool_calls = content, thinking, tool_calls or []


class _OllamaResp(dict):
    def __init__(self, message, **attrs):
        super().__init__(message=message)
        self.__dict__.update(attrs)


def _ollama(*replies):
    provider = OllamaProvider("local", {"model": "qwen3:8b", "host": "http://127.0.0.1:11434"})
    provider.client.chat = AsyncMock(side_effect=[
        _OllamaResp(_OllamaMsg(content, thinking), prompt_eval_count=100, eval_count=4096, done_reason=reason)
        for content, thinking, reason in replies
    ])
    return provider


@pytest.fixture
def _no_daemon(monkeypatch):
    from dpc_client_core.providers import ollama_provider as op
    op._MODEL_INFO.clear()
    monkeypatch.setattr(op.ollama, "Client", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no daemon")))
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    yield
    op._MODEL_INFO.clear()


@pytest.mark.asyncio
async def test_ollama_done_reason_length_counts(tmp_path, _no_daemon):
    provider = _ollama(("the sen", None, "length"), ("tence", None, "stop"))
    text, _, trace = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert text.startswith("the sentence\n\n⚠️ Continued: this reply reached the output limit, after about 4096 tokens")
    assert "turn_end" not in trace


@pytest.mark.asyncio
async def test_ollama_reasoning_handed_over_as_the_answer_is_no_text_when_it_was_cut(tmp_path, _no_daemon):
    """The provider returns the reasoning in place of an empty answer: cut, it would be posted."""
    provider = _ollama(("", "the private reasoning", "length"), ("The answer.", None, "stop"))
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert text == "The answer."
    told = provider.client.chat.await_args_list[1].kwargs["messages"][-1]["content"]
    assert "the whole of it went on reasoning" in str(told)


@pytest.mark.asyncio
async def test_ollama_reasoning_as_the_answer_stays_when_it_was_not_cut(tmp_path, _no_daemon):
    provider = _ollama(("", "the reasoning", "stop"))
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert text == "the reasoning"


def _anthropic(monkeypatch, *replies):
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "k")
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    provider = AnthropicProvider("claude", {"model": "claude-sonnet-4-6", "api_key_env": "TEST_ANTHROPIC_KEY"})
    provider.client = NS(messages=NS(create=AsyncMock(side_effect=[
        NS(content=[NS(type="text", text=text)], stop_reason=reason,
           usage=NS(input_tokens=100, output_tokens=4096, cache_read_input_tokens=0, cache_creation_input_tokens=0))
        for text, reason in replies
    ])))
    return provider


@pytest.mark.asyncio
async def test_anthropic_max_tokens_counts_and_its_words_reach_the_row_as_the_doors_word(tmp_path, monkeypatch):
    provider = _anthropic(monkeypatch, ("the sen", "max_tokens"), ("tence", "end_turn"))
    llm = _adapter(provider, tmp_path)
    text, _, _ = await _through_the_loop(llm, tmp_path)
    assert text.startswith("the sentence\n\n⚠️ Continued")
    assert [row["finish_reason"] for row in llm._ledger.rows()] == ["length", "stop"]


def _gemini(*replies):
    from google.genai import types
    provider = GeminiProvider("gem", {"model": "gemini-2.5-pro", "api_key": "k"})
    reasons = {"MAX_TOKENS": types.FinishReason.MAX_TOKENS, "STOP": types.FinishReason.STOP}
    answers = [
        NS(text=text, candidates=[NS(finish_reason=reasons[reason])],
           usage_metadata=NS(prompt_token_count=100, candidates_token_count=4096, thoughts_token_count=0,
                             cached_content_token_count=0, total_token_count=4196))
        for text, reason in replies
    ]
    provider.client = NS(models=NS(generate_content_stream=lambda **_k: iter([answers.pop(0)]),
                                   generate_content=lambda **_k: answers.pop(0)))
    return provider


@pytest.mark.asyncio
async def test_gemini_max_tokens_counts_on_the_stream_path(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    provider = _gemini(("the sen", "MAX_TOKENS"), ("tence", "STOP"))

    async def _on_chunk(_chunk, _conversation):
        pass

    llm = _adapter(provider, tmp_path)
    text, _, _ = await _through_the_loop(llm, tmp_path, on_stream_chunk=_on_chunk)
    assert text.startswith("the sentence\n\n⚠️ Continued")
    assert [row["finish_reason"] for row in llm._ledger.rows()] == ["length", "stop"]


@pytest.mark.asyncio
async def test_gemini_max_tokens_counts_on_the_plain_path(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    provider = _gemini(("the sen", "MAX_TOKENS"), ("tence", "STOP"))
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert text.startswith("the sentence\n\n⚠️ Continued")


@pytest.mark.asyncio
async def test_a_host_reports_length_on_the_peer_route(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    adapter, asks, _ = _guest(tmp_path, [
        _answer(response="the sen", finish_reason="length", response_tokens=4096),
        _answer(response="tence", finish_reason="stop"),
    ], supports_tools=True)
    text, _, _ = await _through_the_loop(adapter, tmp_path)
    assert text.startswith("the sentence\n\n⚠️ Continued: this reply reached the output limit, after about 4096 tokens")
    assert len(asks) == 2


@pytest.mark.asyncio
async def test_a_cut_call_on_the_peer_route_is_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    block = {"type": "tool_use", "id": "toolu_01", "name": "write_file", "input": {}}
    adapter, _, _ = _guest(tmp_path, [
        _answer(response="", tool_calls=[block], finish_reason="length"),
        _answer(response="In words.", finish_reason="stop"),
    ], supports_tools=True)
    tools = _Tools()
    text, _, _ = await _through_the_loop(adapter, tmp_path, tools=tools)
    assert tools.executed == [] and text == "In words."


def _deepseek(*responses):
    provider = DeepSeekProvider("ds", {"api_key": "k", "model": "deepseek-v4-flash", "thinking": {"enabled": False}})
    provider.client = NS(chat=NS(completions=_Completions(*responses)))
    return provider


def _zai(*responses):
    provider = ZaiProvider("z", {"model": "glm-5.2", "api_key": "k"})
    provider.client = NS(chat=NS(completions=_Completions(*responses)))
    return provider


def _neuraldeep(monkeypatch, *responses):
    provider = NeuralDeepProvider("nd", {"api_key": "k", "model": "qwen3.8-27b"})
    provider.client = NS(chat=NS(completions=_Completions(*responses)))
    monkeypatch.setattr(provider, "_price_usage", AsyncMock())
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("make", ["deepseek", "zai", "neuraldeep"])
async def test_a_provider_whose_usage_says_nothing_keeps_the_old_behaviour(make, tmp_path, monkeypatch):
    """The named gap, VENDOR-ROWS-CARRY-NO-FINISH-REASON-AND-MAX-TOKENS: the word is on the choice
    and never reaches usage, so the text is taken for the whole answer."""
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    cut = _chat_resp("the sen", usage=_openai_usage(), finish_reason="length")
    provider = {"deepseek": lambda: _deepseek(cut), "zai": lambda: _zai(cut),
                "neuraldeep": lambda: _neuraldeep(monkeypatch, cut)}[make]()
    text, _, trace = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert text == "the sen" and "turn_end" not in trace


@pytest.mark.asyncio
@pytest.mark.parametrize("make", ["deepseek", "zai", "neuraldeep", "strata"])
async def test_every_openai_shaped_provider_marks_arguments_that_do_not_parse(make, tmp_path, monkeypatch, _engine_home):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    call = NS(id="c1", function=NS(name="write_file", arguments='{"path": "a.md", "content": "half'))
    first = _chat_resp("", tool_calls=[call], usage=_openai_usage(), finish_reason="length")
    second = _chat_resp("In words.", usage=_openai_usage(5), finish_reason="stop")
    if make == "strata":
        provider, _ = _strata(tmp_path, first, second)
    else:
        provider = {"deepseek": lambda: _deepseek(first, second), "zai": lambda: _zai(first, second),
                    "neuraldeep": lambda: _neuraldeep(monkeypatch, first, second)}[make]()
    tools = _Tools()
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path), tmp_path, tools=tools)
    assert tools.executed == [] and text == "In words."


@pytest.mark.parametrize("arguments", [{"a": 1}, {}, '{"a": 1}'])
def test_arguments_a_provider_handed_over_as_an_object_or_as_json_are_read(arguments):
    block = tool_use_of(NS(id="c", function=NS(name="n", arguments=arguments)))
    assert block.input == (json.loads(arguments) if isinstance(arguments, str) else arguments)
    assert not hasattr(block, "unreadable_arguments")


@pytest.mark.asyncio
async def test_strata_stopped_at_the_window_by_fit_max_tokens_is_not_continued(tmp_path, monkeypatch, _engine_home):
    """`fit_max_tokens` shortens the request to the room left: `length` under the ceiling, at the window."""
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {"context_window": 200000})
    usage = NS(prompt_tokens=195000, completion_tokens=4900, total_tokens=199900, completion_tokens_details=None)
    provider, completions = _strata(tmp_path, _chat_resp("the sen", usage=usage, finish_reason="length"))
    text, _, trace = await _through_the_loop(_adapter(provider, tmp_path, "flash"), tmp_path)
    assert len(completions.bodies) == 1 and trace["turn_end"] == "guard"
    assert "the context window of 200000 tokens" in text and "output limit" not in text


@pytest.mark.asyncio
async def test_ollama_names_its_ceiling_so_the_two_limits_can_be_told_apart(tmp_path, _no_daemon):
    provider = OllamaProvider("local", {"model": "qwen3:8b", "host": "http://127.0.0.1:11434", "num_predict": 4096})
    provider.client.chat = AsyncMock(side_effect=[
        _OllamaResp(_OllamaMsg("the sen"), prompt_eval_count=100, eval_count=4096, done_reason="length"),
        _OllamaResp(_OllamaMsg("tence"), prompt_eval_count=100, eval_count=5, done_reason="stop"),
    ])
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert "reached the output limit of 4096 tokens" in text


@pytest.mark.asyncio
async def test_ollama_with_no_ceiling_and_a_full_window_is_not_continued(tmp_path, monkeypatch, _no_daemon):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {"context_window": 8192})
    provider = OllamaProvider("local", {"model": "qwen3:8b", "host": "http://127.0.0.1:11434", "context_window": 8192})
    provider.client.chat = AsyncMock(side_effect=[
        _OllamaResp(_OllamaMsg("the sen"), prompt_eval_count=4000, eval_count=4192, done_reason="length"),
    ])
    text, _, trace = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert provider.client.chat.await_count == 1 and trace["turn_end"] == "guard"
    assert "the context window of 8192 tokens" in text


@pytest.mark.asyncio
async def test_a_host_at_a_full_window_is_not_continued_on_the_peer_route(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {"context_window": 8192})
    adapter, asks, _ = _guest(tmp_path, [
        _answer(response="the sen", finish_reason="length", prompt_tokens=4000, response_tokens=4192),
    ], supports_tools=True)
    text, _, trace = await _through_the_loop(adapter, tmp_path)
    assert len(asks) == 1 and trace["turn_end"] == "guard" and "the context window of 8192 tokens" in text


@pytest.mark.asyncio
async def test_a_provider_with_no_usage_at_all_keeps_the_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    provider = OpenAICompatibleProvider("oa", {"model": "gpt-x", "api_key": "k"})
    provider.client = NS(chat=NS(completions=_Completions(
        _chat_resp("the sen", usage=_openai_usage(), finish_reason="length"))))
    text, _, trace = await _through_the_loop(_adapter(provider, tmp_path), tmp_path)
    assert text == "the sen" and "turn_end" not in trace


@pytest.mark.asyncio
async def test_ollama_arguments_handed_over_as_a_string_that_does_not_parse_do_not_run(tmp_path, _no_daemon):
    provider = OllamaProvider("local", {"model": "qwen3:8b", "host": "http://127.0.0.1:11434"})
    call = NS(id="c1", function=NS(name="write_file", arguments='{"content": "half'))
    provider.client.chat = AsyncMock(side_effect=[
        _OllamaResp(_OllamaMsg("", None, [call]), prompt_eval_count=100, eval_count=4096, done_reason="stop"),
        _OllamaResp(_OllamaMsg("In words."), prompt_eval_count=100, eval_count=5, done_reason="stop"),
    ])
    tools = _Tools()
    text, _, _ = await _through_the_loop(_adapter(provider, tmp_path), tmp_path, tools=tools)
    assert tools.executed == [] and text == "In words."
