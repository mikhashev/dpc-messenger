"""Who may tell you who is in a group, and whose history you will believe.

Two doors were open, and each made the other's gate pointless:

  - GROUP_SYNC reached `apply_sync` without the sender's identity ever being
    passed, so any connected peer could rewrite a roster by bidding a higher
    version — and a phantom member it added would never advertise a capability,
    pinning the group in soft mode for good;
  - CHAT_HISTORY_RESPONSE was accepted unsolicited, so gating the *request*
    changed nothing: an unasked-for reply replaced the whole local history.

GROUP_DELETE checks the creator and GROUP_LEAVE only concerns its sender. The
most powerful operation of the three checked nothing.
"""

import pytest

from dpc_client_core.managers.group_manager import GroupManager
from dpc_client_core.message_handlers.group_handler import GroupSyncHandler
from dpc_client_core.message_handlers.chat_history_handlers import (
    ChatHistoryResponseHandler,
    HistoryRequestRegistry,
)


MEMBER = "dpc-node-1111111111111111111111111111aaaa"
OUTSIDER = "dpc-node-2222222222222222222222222222bbbb"
SELF = "dpc-node-3333333333333333333333333333cccc"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _P2P:
    def __init__(self):
        self.node_id = SELF
        self.peers = {}
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Monitor:
    def __init__(self):
        self.imported = None
        self.message_history = [{"id": "local-1", "content": "the original"}]

    def import_history(self, messages):
        self.imported = messages
        self.message_history = list(messages)


class _Service:
    """A stand-in for CoreService that borrows the real registry.

    Reimplementing the registry here would only prove the stub agrees with
    itself — the double has to run the code under test.
    """

    def __init__(self, group_manager):
        self.group_manager = group_manager
        self.local_api = _Api()
        self.p2p_manager = _P2P()
        self.conversation_monitors = {}
        self.knowledge_service = None
        self.history_requests = HistoryRequestRegistry()


@pytest.fixture
def group_manager(tmp_path):
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    group = manager.create_group("work", "topic", [MEMBER])
    return manager, group


# --- door 1: who may rewrite the roster ----------------------------------

@pytest.mark.asyncio
async def test_an_outsider_cannot_rewrite_the_roster(group_manager):
    """A peer not in the group has no say in who is."""
    manager, group = group_manager
    service = _Service(manager)
    handler = GroupSyncHandler(service)

    forged = group.to_dict()
    forged["version"] = 10 ** 6
    forged["members"] = [OUTSIDER, "dpc-node-phantom"]

    await handler.handle(OUTSIDER, forged)

    assert manager.get_group(group.group_id).members == group.members


@pytest.mark.asyncio
async def test_a_member_may_still_sync(group_manager):
    """The gate must not break the flow it protects."""
    manager, group = group_manager
    service = _Service(manager)
    handler = GroupSyncHandler(service)

    update = group.to_dict()
    update["version"] = group.version + 1
    update["topic"] = "renamed by a member"

    await handler.handle(MEMBER, update)

    assert manager.get_group(group.group_id).topic == "renamed by a member"


@pytest.mark.asyncio
async def test_a_sync_for_an_unknown_group_does_not_create_one(group_manager):
    """An invitation is GROUP_CREATE. A sync is not a way in."""
    manager, _ = group_manager
    service = _Service(manager)
    handler = GroupSyncHandler(service)

    await handler.handle(OUTSIDER, {
        "group_id": "group-fabricated",
        "name": "not yours",
        "version": 10 ** 6,
        "members": [OUTSIDER, SELF],
        "created_by": OUTSIDER,
    })

    assert manager.get_group("group-fabricated") is None


# --- door 2: whose history you believe -----------------------------------

@pytest.mark.asyncio
async def test_unsolicited_history_does_not_replace_the_local_one(group_manager):
    """Gating the request is pointless while the reply needs no request."""
    manager, group = group_manager
    service = _Service(manager)
    monitor = _Monitor()
    service.conversation_monitors[group.group_id] = monitor
    handler = ChatHistoryResponseHandler(service)

    await handler.handle(OUTSIDER, {
        "conversation_id": group.group_id,
        "request_id": "never-asked",
        "messages": [{"id": "forged-1", "content": "a history that never happened"}],
        "total_count": 1,
    })

    assert monitor.imported is None
    assert monitor.message_history == [{"id": "local-1", "content": "the original"}]


@pytest.mark.asyncio
async def test_the_history_we_asked_for_is_accepted(group_manager):
    """And the answer to a real question still arrives."""
    manager, group = group_manager
    service = _Service(manager)
    monitor = _Monitor()
    service.conversation_monitors[group.group_id] = monitor
    handler = ChatHistoryResponseHandler(service)

    service.history_requests.note(MEMBER, group.group_id, "req-1")

    await handler.handle(MEMBER, {
        "conversation_id": group.group_id,
        "request_id": "req-1",
        "messages": [{"id": "peer-1", "content": "what was said"}],
        "total_count": 1,
    })

    assert monitor.imported == [{"id": "peer-1", "content": "what was said"}]


@pytest.mark.asyncio
async def test_the_answer_must_come_from_the_peer_we_asked(group_manager):
    """Asking A is not permission for B to answer."""
    manager, group = group_manager
    service = _Service(manager)
    monitor = _Monitor()
    service.conversation_monitors[group.group_id] = monitor
    handler = ChatHistoryResponseHandler(service)

    service.history_requests.note(MEMBER, group.group_id, "req-1")

    await handler.handle(OUTSIDER, {
        "conversation_id": group.group_id,
        "request_id": "req-1",
        "messages": [{"id": "forged-1", "content": "intercepted"}],
        "total_count": 1,
    })

    assert monitor.imported is None


@pytest.mark.asyncio
async def test_a_request_is_answered_once(group_manager):
    """A single question does not license a stream of answers."""
    manager, group = group_manager
    service = _Service(manager)
    monitor = _Monitor()
    service.conversation_monitors[group.group_id] = monitor
    handler = ChatHistoryResponseHandler(service)

    service.history_requests.note(MEMBER, group.group_id, "req-1")
    payload = {
        "conversation_id": group.group_id,
        "request_id": "req-1",
        "messages": [{"id": "peer-1", "content": "first"}],
        "total_count": 1,
    }
    await handler.handle(MEMBER, payload)

    second = dict(payload, messages=[{"id": "peer-2", "content": "second"}])
    await handler.handle(MEMBER, second)

    assert monitor.message_history == [{"id": "peer-1", "content": "first"}]
