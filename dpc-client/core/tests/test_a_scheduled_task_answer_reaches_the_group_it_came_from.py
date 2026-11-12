"""A task scheduled in a group posts its answer there, whatever its type.

The post used to sit inside the check_back branch, so a chat, review or
reminder task scheduled from a group produced its answer and dropped it.
"""
import asyncio
from types import SimpleNamespace as NS

import pytest

from dpc_client_core.dpc_agent import events
from dpc_client_core.dpc_agent.agent import DpcAgent

GROUP = "group-b88b65076b85"


class _Service:
    def __init__(self):
        self.posts = []

    async def send_group_agent_message(self, group_id, agent_name, text, **kw):
        self.posts.append({"group_id": group_id, "agent_name": agent_name, "text": text, **kw})
        return "msg-1"


def _agent(service, answer="the answer", trace=None, handlers=None):
    agent = DpcAgent.__new__(DpcAgent)
    agent._run_gate = asyncio.Lock()
    agent._service = service
    agent.display_name = "Ark"
    agent.agent_root = NS(name="agent_001")
    agent._task_handlers = handlers or {}
    agent._telegram_send_fn = None
    agent._convert_task_data_to_prompt = lambda d: ""
    agent.process_calls = []

    async def _process(text, conversation_id=None, **kw):
        agent.process_calls.append(conversation_id)
        agent._last_trace = trace or {}
        return answer

    agent.process = _process
    return agent


def _task(task_type, origin, **data):
    return NS(id="task-1", task_type=task_type,
              data={"_reply_conversation_id": origin, **data})


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type,data", [
    ("chat", {"text": "say hello"}),
    ("check_back", {"text": "look again", "_check_back_depth": 1}),
    ("review", {"target": "dpc_agent/agent.py"}),
])
async def test_a_task_with_a_group_origin_posts_its_answer_there_once(task_type, data):
    service = _Service()
    agent = _agent(service)

    outcome = await DpcAgent._execute_task(agent, _task(task_type, GROUP, **data))

    assert outcome == "the answer"
    assert len(service.posts) == 1, service.posts
    assert service.posts[0]["group_id"] == GROUP
    assert service.posts[0]["text"] == "the answer"
    assert service.posts[0]["agent_name"] == "Ark"


@pytest.mark.asyncio
async def test_a_reminder_with_a_group_origin_posts_the_reminder(monkeypatch):
    async def _no_event(*a, **kw):
        return None

    monkeypatch.setattr(events, "emit_agent_message", _no_event)
    service = _Service()
    agent = _agent(service)

    await DpcAgent._execute_task(agent, _task("reminder", GROUP, message="stand-up"))

    assert [p["text"] for p in service.posts] == ["⏰ Reminder: stand-up"]
    assert service.posts[0]["group_id"] == GROUP
    assert agent.process_calls == [], "a reminder does not run the model"


@pytest.mark.asyncio
async def test_a_registered_task_type_with_a_group_origin_posts_its_answer():
    service = _Service()

    async def _handler(data):
        return "improved"

    agent = _agent(service, handlers={"improvement": _handler})

    await DpcAgent._execute_task(agent, _task("improvement", GROUP))

    assert [p["text"] for p in service.posts] == ["improved"]


@pytest.mark.asyncio
@pytest.mark.parametrize("task_type,data", [
    ("chat", {"text": "say hello"}),
    ("check_back", {"text": "look again"}),
    ("review", {"target": "x"}),
])
async def test_a_one_to_one_origin_posts_nothing_to_any_group(task_type, data):
    service = _Service()
    agent = _agent(service)

    await DpcAgent._execute_task(agent, _task(task_type, "agent_001", **data))

    assert service.posts == []


@pytest.mark.asyncio
async def test_a_task_with_no_origin_posts_nothing():
    service = _Service()
    agent = _agent(service)

    await DpcAgent._execute_task(agent, NS(id="task-3", task_type="chat", data={"text": "x"}))

    assert service.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_end", ["stopped", "killed", "failed", "refused", "guard"])
async def test_a_turn_that_ended_without_an_answer_carries_its_turn_end(turn_end):
    service = _Service()
    agent = _agent(service, trace={"turn_end": turn_end})

    await DpcAgent._execute_task(agent, _task("chat", GROUP, text="x"))

    assert service.posts[0]["turn_end"] == turn_end


@pytest.mark.asyncio
async def test_a_stale_turn_end_from_an_earlier_turn_does_not_label_a_reminder(monkeypatch):
    async def _no_event(*a, **kw):
        return None

    monkeypatch.setattr(events, "emit_agent_message", _no_event)
    service = _Service()
    agent = _agent(service)
    agent._last_trace = {"turn_end": "failed"}

    await DpcAgent._execute_task(agent, _task("reminder", GROUP, message="m"))

    assert "turn_end" not in service.posts[0]


@pytest.mark.asyncio
async def test_a_failed_handler_and_an_unknown_type_post_no_error_line():
    service = _Service()

    async def _boom(data):
        raise RuntimeError("boom")

    agent = _agent(service, handlers={"custom": _boom})

    await DpcAgent._execute_task(agent, _task("custom", GROUP))
    await DpcAgent._execute_task(agent, _task("no-such-type", GROUP))
    await DpcAgent._execute_task(agent, _task("review", GROUP))

    assert service.posts == []


@pytest.mark.asyncio
async def test_a_deleted_origin_group_is_said_in_the_outcome_and_the_log(caplog):
    """send_group_agent_message returns None for a group that no longer exists;
    that is not a post, so the queue must not record a report nobody saw."""
    class _GoneService:
        async def send_group_agent_message(self, group_id, agent_name, text, **kw):
            return None

    agent = _agent(_GoneService())

    with caplog.at_level("WARNING"):
        outcome = await DpcAgent._execute_task(
            agent, _task("chat", GROUP, text="say hello"))

    assert outcome.startswith(f"[not posted to {GROUP}:"), outcome
    assert outcome.endswith("the answer")
    assert any(r.levelname == "WARNING" and "refused" in r.getMessage() and GROUP in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]
