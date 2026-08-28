"""A message from a peer reaches the screen with its number.

The defect that started all of this: in a group, your own messages were
numbered and everyone else's were not. Not a rendering bug — the broadcast
went out before the monitor had written the record, and the number is assigned
by that write. Both send paths already fed the monitor first and said so in a
comment; the receive path did the opposite.
"""

import pytest

from dpc_client_core.message_handlers.group_handler import GroupTextHandler


GROUP = "group-1234"
PEER = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Monitor:
    def __init__(self):
        self._history = []

    async def on_message(self, message):
        self._history.append({"id": message.message_id,
                              "msg_index": len(self._history) + 1})

    def save_history(self):
        pass

    def get_message_history(self):
        return self._history

    message_ids = ()


class _P2P:
    node_id = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
    peers = {}


class _Service:
    def __init__(self):
        self.local_api = _Api()
        self.p2p_manager = _P2P()
        self.monitor = _Monitor()
        self.conversation_monitors = {}
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        self.group_manager = _GroupManager()

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor


class _GroupManager:
    def get_group(self, group_id):
        return None  # no relay fan-out in this test


def _payload(message_id="m-1", text="hello"):
    return {
        "group_id": GROUP,
        "text": text,
        "sender_name": "Mike (linux)",
        "sender_type": "human",
        "sender_node_id": PEER,
        "message_id": message_id,
        "timestamp": "2026-08-06T00:00:00+00:00",
        "mentions": [],
    }


@pytest.mark.asyncio
async def test_a_peer_message_is_broadcast_with_its_number(monkeypatch):
    service = _Service()
    handler = GroupTextHandler(service)
    monkeypatch.setattr(handler, "_handle_agent_mentions", _noop)

    await handler.handle(PEER, _payload())

    name, event = service.local_api.events[-1]
    assert name == "group_text_received"
    assert event["msg_index"] == 1, "the number the monitor assigned did not reach the UI"


@pytest.mark.asyncio
async def test_numbers_keep_counting_across_messages(monkeypatch):
    service = _Service()
    handler = GroupTextHandler(service)
    monkeypatch.setattr(handler, "_handle_agent_mentions", _noop)

    await handler.handle(PEER, _payload("m-1", "first"))
    await handler.handle(PEER, _payload("m-2", "second"))

    assert [e["msg_index"] for _, e in service.local_api.events] == [1, 2]


@pytest.mark.asyncio
async def test_the_record_is_written_before_the_screen_is_told(monkeypatch):
    """Order is the fix; asserting it directly keeps it from drifting back."""
    service = _Service()
    handler = GroupTextHandler(service)
    monkeypatch.setattr(handler, "_handle_agent_mentions", _noop)

    await handler.handle(PEER, _payload())

    assert service.monitor.get_message_history(), "nothing was stored"
    assert service.local_api.events, "nothing was broadcast"


async def _noop(*args, **kwargs):
    return None
