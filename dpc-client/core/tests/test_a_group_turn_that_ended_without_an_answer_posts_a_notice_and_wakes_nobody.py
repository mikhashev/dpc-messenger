"""A group turn that ended without an answer: what the group reads, and who it wakes.

A failed run's text is the raw exception (`⚠️ LLM error: {e}`, `⚠️ Loop error: {e}`),
and a group record carries no `turn_end`, so nothing caps it: posted, it is
signed, sent to every member, stored on every disk and replayed whole into
every agent's prompt. The group gets `FAILED_TURN_NOTICE`; the error goes to
the owner's UI only. A stopped run's text is the model's own summary and is
posted as it is — but a stop ends the work, so neither post wakes an agent it
names on this node.

The door and its three callers are the real methods bound to a stub service;
the mention router is real too, down to the call that would start the run.
"""

import asyncio
from types import SimpleNamespace as NS

import pytest

from dpc_client_core.dpc_agent.agent import DpcAgent
from dpc_client_core.dpc_agent.context import FAILED_TURN_NOTICE
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from dpc_client_core.service import CoreService


NODE = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
GROUP = "group-1234"
RAW_ERROR = ("⚠️ LLM error: 502 Bad Gateway <html><body>upstream at "
             "https://provider.example.invalid/v1?key=secret-token-123</body></html>")
STOP_SUMMARY = "⚠️ Stopped by user. I read two files; @Muse should take the rest, and @CC too."
ANSWER = "Done. @Muse, your turn — and @CC, look at it."


class _Group:
    group_id = GROUP
    members = [NODE]
    name = "Project room"
    agents = {NODE: ["agent_001", "agent_002"]}
    agent_names = {NODE: {"agent_001": "Ark", "agent_002": "Muse"}}


class _GroupManager:
    def get_group(self, group_id):
        return _Group() if group_id == GROUP else None


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Monitor:
    def __init__(self):
        self.seen = []
        self._history = []

    async def on_message(self, message):
        self.seen.append(message)
        self._history.append({"id": message.message_id, "content": message.text,
                              "msg_index": len(self._history) + 1,
                              "sender_name": message.sender_name})

    def save_history(self):
        pass

    def get_message_history(self):
        return self._history

    def get_last_msg_index(self):
        return self._history[-1]["msg_index"] if self._history else 0

    def set_token_count(self, _):
        pass

    def get_token_usage(self):
        return {"token_limit": 128000}


class _Manager:
    """A manager whose run ends the way `trace` says, as process_message leaves it."""

    def __init__(self, response, trace):
        self._response = response
        self._trace = trace
        self._last_used_agent = None

    async def process_message(self, **_kw):
        self._last_used_agent = NS(_last_trace=self._trace)
        return self._response


class _Service:
    """Only the collaborators are stubbed; the door and the router are real."""

    def __init__(self, manager=None):
        self.group_manager = _GroupManager()
        self.p2p_manager = NS(node_id=NODE, peers={})
        self.local_api = _Api()
        self.monitor = _Monitor()
        self.broadcast = []
        self.woken = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self._manager = manager

        async def _ensure_manager(_agent_id):
            return manager

        self.llm_manager = NS(providers={"dpc_agent": NS(
            _ensure_manager=_ensure_manager, get_manager=lambda _agent_id: manager)})

    send_group_agent_message = CoreService.send_group_agent_message
    _handle_group_agent_mentions = CoreService._handle_group_agent_mentions
    _invoke_agent_in_group = CoreService._invoke_agent_in_group
    _signature_fields_for = staticmethod(CoreService._signature_fields_for)
    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as

    async def _invoke_agent_in_group_serialized(self, group_id, text, sender_name, agent_id,
                                                trigger_message_id=None, mention_depth=0):
        self.woken.append(agent_id)

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    def _group_chat_context(self, group_id):
        return None

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []

    def _get_default_agent_id(self):
        return "agent_001"

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Ark", "agent_002": "Muse"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"


def _posted(service):
    assert service.broadcast, "nothing was sent to the group"
    return service.broadcast[-1]["payload"]


def _toasts(service):
    return [p for name, p in service.local_api.events if name == "error_toast"]


def _bridge_wakes(service):
    return [p for name, p in service.local_api.events if name == "cc_group_mention"]


async def _settled():
    await asyncio.sleep(0)  # the router starts embedded runs with ensure_future


def _check_failed_post(service):
    payload = _posted(service)
    assert payload["text"] == FAILED_TURN_NOTICE
    for held in (payload["text"], service.monitor.seen[-1].text,
                 [e for e in service.local_api.events if e[0] == "group_text_received"][-1][1]["text"]):
        assert "secret-token-123" not in held and "502" not in held
    toasts = _toasts(service)
    assert len(toasts) == 1, "the owner was not told why"
    assert RAW_ERROR in toasts[0]["message"]
    assert "Ark" in toasts[0]["message"] and "Project room" in toasts[0]["message"]


# --- the door ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_failed_turn_posts_the_notice_and_its_error_goes_to_the_owner_only():
    service = _Service()

    await service.send_group_agent_message(GROUP, "Ark", RAW_ERROR, turn_end="failed")

    _check_failed_post(service)


@pytest.mark.asyncio
async def test_a_stopped_turn_posts_its_summary_as_it_is_and_wakes_nobody():
    service = _Service()

    await service.send_group_agent_message(GROUP, "Ark", STOP_SUMMARY, turn_end="stopped")
    await _settled()

    assert _posted(service)["text"] == STOP_SUMMARY
    assert service.monitor.seen[-1].text == STOP_SUMMARY
    assert _toasts(service) == []
    assert service.woken == [], "a stop summary started the agent it named"
    assert _bridge_wakes(service) == [], "a stop summary woke the bridge it named"


@pytest.mark.asyncio
async def test_a_failed_turn_that_names_an_agent_wakes_nobody():
    service = _Service()

    await service.send_group_agent_message(GROUP, "Ark", RAW_ERROR + " @Muse @CC", turn_end="failed")
    await _settled()

    assert service.woken == [] and _bridge_wakes(service) == []


@pytest.mark.asyncio
async def test_an_answer_is_posted_and_routes_its_mentions_as_before():
    service = _Service()

    await service.send_group_agent_message(GROUP, "Ark", ANSWER)
    await _settled()

    assert _posted(service)["text"] == ANSWER
    assert _toasts(service) == []
    assert service.woken == ["agent_002"]
    assert [p["agent_tag"] for p in _bridge_wakes(service)] == ["cc"]


@pytest.mark.asyncio
async def test_an_unknown_turn_end_is_posted_as_an_answer():
    """A bridge's value arrives through the local API unchecked."""
    service = _Service()

    await service.send_group_agent_message(GROUP, "Ark", ANSWER, turn_end="no-such-end")
    await _settled()

    assert _posted(service)["text"] == ANSWER
    assert service.woken == ["agent_002"]


# --- the callers ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_mention_on_this_node_passes_the_turns_end_to_the_door():
    service = _Service(_Manager(RAW_ERROR, {"turn_end": "failed"}))

    await service._invoke_agent_in_group(GROUP, "@Ark look", "Mike", "agent_001")

    _check_failed_post(service)


@pytest.mark.asyncio
async def test_a_mention_on_this_node_that_was_stopped_wakes_nobody():
    service = _Service(_Manager(STOP_SUMMARY, {"turn_end": "stopped"}))

    await service._invoke_agent_in_group(GROUP, "@Ark look", "Mike", "agent_001")
    await _settled()

    assert _posted(service)["text"] == STOP_SUMMARY
    assert service.woken == [] and _bridge_wakes(service) == []


@pytest.mark.asyncio
async def test_a_mention_from_a_peer_passes_the_turns_end_to_the_door():
    service = _Service(_Manager(RAW_ERROR, {"turn_end": "failed"}))
    handler = GroupTextHandler(service)

    await handler._invoke_agent(GROUP, "@Ark look", "Peer")

    _check_failed_post(service)


@pytest.mark.asyncio
async def test_a_mention_from_a_peer_that_was_stopped_wakes_nobody():
    service = _Service(_Manager(STOP_SUMMARY, {"turn_end": "stopped"}))
    handler = GroupTextHandler(service)

    await handler._invoke_agent(GROUP, "@Ark look", "Peer")
    await _settled()

    assert _posted(service)["text"] == STOP_SUMMARY
    assert service.woken == [] and _bridge_wakes(service) == []


def _check_back_agent(service, response, trace):
    agent = DpcAgent.__new__(DpcAgent)
    agent._run_gate = asyncio.Lock()
    agent._service = service
    agent.display_name = "Ark"
    agent.agent_root = NS(name="agent_001")
    agent._task_handlers = {}
    agent._telegram_send_fn = None
    agent._convert_task_data_to_prompt = lambda d: ""

    async def _process(text, conversation_id=None, **_kw):
        agent._last_trace = trace
        return response

    agent.process = _process
    return agent


def _check_back_task():
    return NS(id="task-1", task_type="check_back",
              data={"text": "look again", "_reply_conversation_id": GROUP, "_check_back_depth": 1})


@pytest.mark.asyncio
async def test_a_check_back_that_failed_posts_the_notice():
    service = _Service()
    agent = _check_back_agent(service, RAW_ERROR, {"turn_end": "failed"})

    outcome = await DpcAgent._execute_task(agent, _check_back_task())

    _check_failed_post(service)
    assert outcome == RAW_ERROR, "the task log is the owner's and keeps the error"


@pytest.mark.asyncio
async def test_a_check_back_that_was_stopped_wakes_nobody():
    service = _Service()
    agent = _check_back_agent(service, STOP_SUMMARY, {"turn_end": "stopped"})

    await DpcAgent._execute_task(agent, _check_back_task())
    await _settled()

    assert _posted(service)["text"] == STOP_SUMMARY
    assert service.woken == [] and _bridge_wakes(service) == []


@pytest.mark.asyncio
async def test_a_check_back_answer_routes_its_mentions_as_before():
    service = _Service()
    agent = _check_back_agent(service, ANSWER, {})

    await DpcAgent._execute_task(agent, _check_back_task())
    await _settled()

    assert _posted(service)["text"] == ANSWER
    assert service.woken == ["agent_002"]
