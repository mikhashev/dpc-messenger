"""A scheduled task's prompt leaves the Telegram forwarding of a CC chain as it was.

A CC-chain answer in an agent chat is forwarded to Telegram when the last user
record that is not CC's came from Telegram (`source == "telegram"`). A check_back
or chat task that answers in a 1:1 agent chat now records its prompt there too,
marked with `task_type`. Read as the person's input it would switch the
forwarding off after a Telegram conversation, though nobody spoke from anywhere,
so the scan skips it as it skips CC's records.

The prompt is written by the agent manager's own `_record_prompt` into a real
ConversationMonitor; the service is a bare `CoreService` with doubles for the
agent manager, the local API and the bridge. Nothing reaches the real ~/.dpc.
"""

import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.managers.agent_manager import DpcAgentManager
from dpc_client_core.service import CoreService

AGENT = "agent_telegram_fwd_0001"
NODE = "dpc-node-0000000000000000000000000000beef"
ANSWER = "Ark: the render is done."


class _Bridge:
    def __init__(self):
        self.sent = []

    async def send_chain_response(self, text):
        self.sent.append(text)


class _LocalApi:
    async def broadcast_event(self, *args, **kwargs):
        pass


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    m = ConversationMonitor(
        conversation_id=AGENT,
        participants=[{"node_id": NODE, "name": "User", "context": "local"}],
        llm_manager=None,
    )
    m._get_history_path = lambda: tmp_path / AGENT / "history.json"
    return m


def _scheduled_prompt(monitor):
    owner = SimpleNamespace(service=SimpleNamespace(p2p_manager=SimpleNamespace(node_id=NODE)))
    DpcAgentManager._record_prompt(owner, monitor, "look at the render again",
                                   "Scheduled check_back", task_type="check_back")
    monitor.add_message(role="assistant", content="Still rendering.", sender_node_id=AGENT)


async def _cc_chain_answer(monitor):
    """CC's record as send_cc_agent_response saves it, then the agent's answer."""
    monitor.add_message(role="user", content="@Ark check it", sender_node_id="cc",
                        sender_name="CC")

    async def process_message(**kwargs):
        monitor.add_message(role="assistant", content=ANSWER, sender_node_id=AGENT)
        return ANSWER

    manager = SimpleNamespace(_agent_monitors={AGENT: monitor}, process_message=process_message)
    bridge = _Bridge()
    service = CoreService.__new__(CoreService)
    service.local_api = _LocalApi()
    service.get_cc_display_name = lambda: "CC"
    service._get_agent_display_name = lambda conversation_id=None: "Ark"
    service._get_telegram_bridge_for_conversation = lambda conversation_id: bridge
    await service._invoke_agent_in_agent_chat(AGENT, "@Ark check it", manager)
    return bridge.sent


@pytest.mark.asyncio
async def test_a_scheduled_prompt_after_telegram_keeps_the_chain_answer_going_to_telegram(monitor):
    monitor.add_message(role="user", content="how is the render?", sender_node_id=NODE,
                        sender_name="Mike", source="telegram")
    monitor.add_message(role="assistant", content="Rendering.", sender_node_id=AGENT)
    _scheduled_prompt(monitor)

    prompt = [r for r in monitor.message_history if r.get("task_type")]
    assert len(prompt) == 1 and "source" not in prompt[0]
    assert await _cc_chain_answer(monitor) == [ANSWER]


@pytest.mark.asyncio
async def test_a_scheduled_prompt_alone_does_not_send_the_chain_answer_to_telegram(monitor):
    _scheduled_prompt(monitor)

    assert await _cc_chain_answer(monitor) == []
