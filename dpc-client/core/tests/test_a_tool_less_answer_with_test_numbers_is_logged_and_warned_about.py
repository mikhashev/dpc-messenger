"""A work request answered in ONE LLM call with no tool call, carrying test numbers no tool produced.

In the group chat an agent answered a review request with `rounds: 1`, `tool_calls=0`, `finish=stop`
and published "553 passed". No tool ran, so no test run could have made the number. Decided with Mike:
log it, never change the loop. (a) Every turn that ends in round 1 with zero tool calls is logged;
(b) when its final answer contains test-run-looking numbers (`N passed`, `N failed`, `N deselected`,
...) a WARNING names the agent, the conversation, the rounds and the matched snippets. No retry, no
change to the loop's behaviour or to the answer text; the task result record carries the boolean
`unbacked_test_numbers` (and the `tool_calls` count) beside `rounds`.

The pure reader and the logging helper are tested alone; `DpcAgent.process` is tested over a double
of the loop, with its real record writer.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dpc_client_core.dpc_agent import agent as agent_mod
from dpc_client_core.dpc_agent import loop as loop_mod
from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.loop import note_a_tool_less_turn, numbers_of_a_test_run_in as run_numbers_in
from dpc_client_core.dpc_agent.memory import Memory

from tests.test_the_morning_brief_rides_once_in_the_first_turn_tail import world  # noqa: F401

AGENT = "agent_001"
REVIEW = "Review done. 553 passed, 2 failed, 4 deselected in 31.2s - all good."


def _usage(rounds=1):
    return {"rounds": rounds}


def _trace(tools=0):
    calls = [{"tool": "run_shell", "args": {}, "result": "ok", "is_error": False} for _ in range(tools)]
    return {"tool_calls": calls, "accumulated_tool_calls": list(calls)}


class TestWhatLooksLikeATestRun:
    @pytest.mark.parametrize("text, expected", [
        ("553 passed", ["553 passed"]),
        ("553 passed, 2 failed, 4 deselected in 31s", ["553 passed", "2 failed", "4 deselected"]),
        ("398 PASSED", ["398 PASSED"]),
        ("1 passed", ["1 passed"]),
        ("5 skipped", ["5 skipped"]),
    ])
    def test_the_counts_of_a_run_are_found(self, text, expected):
        assert run_numbers_in(text) == expected

    @pytest.mark.parametrize("text", [
        "", None, "All tests pass.", "the build passed review", "passed 3 checks", "I have 3 files, all passing",
        "version 1.2.3 failed_over", "ids 12345passed",
    ])
    def test_prose_that_only_resembles_one_is_not(self, text):
        assert run_numbers_in(text) == []

    def test_the_same_snippet_is_reported_once(self):
        assert run_numbers_in("12 passed ... again 12 passed") == ["12 passed"]


class TestTheLogLine:
    def test_a_one_round_turn_with_no_tool_is_logged_and_a_plain_answer_is_not_warned_about(self, caplog):
        with caplog.at_level(logging.INFO, logger=loop_mod.log.name):
            flagged = note_a_tool_less_turn("Sure, here is a summary.", _usage(), _trace(),
                                            agent_id=AGENT, conversation_id="group-1")
        assert flagged is False
        info = [r for r in caplog.records if r.levelno == logging.INFO and "tool-less turn" in r.getMessage().lower()]
        assert len(info) == 1
        said = info[0].getMessage()
        assert AGENT in said and "group-1" in said and "rounds=1" in said and "tool_calls=0" in said
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_test_numbers_in_such_an_answer_are_a_warning_with_the_snippets(self, caplog):
        with caplog.at_level(logging.INFO, logger=loop_mod.log.name):
            flagged = note_a_tool_less_turn(REVIEW, _usage(), _trace(), agent_id=AGENT, conversation_id="group-1")
        assert flagged is True
        (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
        said = warning.getMessage()
        assert AGENT in said and "group-1" in said and "rounds=1" in said and "tool_calls=0" in said
        assert "553 passed" in said and "2 failed" in said and "4 deselected" in said

    @pytest.mark.parametrize("usage, trace", [
        (_usage(2), _trace()),       # a second round: the loop went on
        (_usage(1), _trace(1)),      # a tool ran: the number may be its
        (_usage(0), _trace()),       # nothing was asked of the model
    ])
    def test_a_turn_that_used_a_tool_or_a_second_round_is_neither_logged_nor_warned(self, caplog, usage, trace):
        with caplog.at_level(logging.DEBUG, logger=loop_mod.log.name):
            flagged = note_a_tool_less_turn(REVIEW, usage, trace, agent_id=AGENT, conversation_id="group-1")
        assert flagged is False
        assert not [r for r in caplog.records if "tool-less" in r.getMessage().lower()]

    def test_a_trace_without_a_tool_list_is_read_as_no_tool(self, caplog):
        with caplog.at_level(logging.INFO, logger=loop_mod.log.name):
            assert note_a_tool_less_turn(REVIEW, _usage(), {}, agent_id=AGENT, conversation_id=None) is True

    def test_a_logging_failure_never_reaches_the_turn(self, monkeypatch):
        monkeypatch.setattr(loop_mod, "log", MagicMock(info=MagicMock(side_effect=OSError("disk"))))
        assert note_a_tool_less_turn(REVIEW, _usage(), _trace(), agent_id=AGENT, conversation_id="c") is False


# The agent wired to it: the real writer of the task result, the loop a double.


class _Loop:
    def __init__(self, answer, rounds=1, tools=0):
        self.answer, self.rounds, self.tools = answer, rounds, tools

    async def __call__(self, **kwargs):
        return self.answer, {"rounds": self.rounds}, _trace(self.tools)


def _agent(world, monkeypatch, loop):  # noqa: F811
    root, _ = world
    monkeypatch.setattr(agent_mod, "run_llm_loop", loop)

    async def _no_recall(*a, **k):
        return []

    monkeypatch.setattr(agent_mod, "recall_off_loop", _no_recall)
    monkeypatch.setattr("dpc_client_core.dpc_agent.loop.load_agent_config", lambda _n: {})
    agent = DpcAgent.__new__(DpcAgent)
    agent.agent_root = root
    agent.memory = Memory(root)
    agent.skill_store = agent._embedding_provider = agent._firewall = None
    agent._firewall_profile = None
    agent._service = None
    agent.tools = MagicMock()
    agent.llm = SimpleNamespace()
    agent.skill_reflector = MagicMock()
    agent.skill_reflector.record_outcome.return_value = []
    agent.config = SimpleNamespace(billing_model="pay_per_use", budget_usd=None, max_rounds=1)
    agent._get_allowed_tools = lambda **kw: set()
    agent._runtime_billing_model = lambda: None
    agent._provider_facts = lambda: {}
    agent._update_usage_stats = lambda **kw: None
    agent._last_usage = {}
    agent._last_trace = {}
    return agent


def _record(root, task_id):
    return json.loads((root / "task_results" / f"{task_id}.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_the_task_result_records_that_the_answer_carried_unbacked_numbers(world, monkeypatch, caplog):  # noqa: F811
    root, _ = world
    agent = _agent(world, monkeypatch, _Loop(REVIEW))
    with caplog.at_level(logging.INFO, logger=loop_mod.log.name):
        text = await agent.process("review please", "group-1", message_source="ui", task_id="chat-aaaa1111")
    assert text == REVIEW, "the answer text is not touched"
    record = _record(root, "chat-aaaa1111")
    assert record["rounds"] == 1 and record["tool_calls"] == 0
    assert record["unbacked_test_numbers"] is True
    assert record["response"] == REVIEW
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "553 passed" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_plain_tool_less_answer_records_false(world, monkeypatch):  # noqa: F811
    root, _ = world
    agent = _agent(world, monkeypatch, _Loop("fine, thanks"))
    await agent.process("hello", "group-1", message_source="ui", task_id="chat-bbbb2222")
    record = _record(root, "chat-bbbb2222")
    assert record["unbacked_test_numbers"] is False and record["tool_calls"] == 0


@pytest.mark.asyncio
async def test_numbers_after_a_tool_ran_are_not_flagged_and_the_count_is_recorded(world, monkeypatch):  # noqa: F811
    root, _ = world
    agent = _agent(world, monkeypatch, _Loop(REVIEW, rounds=2, tools=1))
    await agent.process("run the suite", "group-1", message_source="ui", task_id="chat-cccc3333")
    record = _record(root, "chat-cccc3333")
    assert record["unbacked_test_numbers"] is False and record["tool_calls"] == 1 and record["rounds"] == 2
