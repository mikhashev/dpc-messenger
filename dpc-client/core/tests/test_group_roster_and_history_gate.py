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
        self.denials_cleared = []

    def clear_group_access_denied(self, peer_id, group_id):
        self.denials_cleared.append((peer_id, group_id))

    def note_group_access_denied(self, peer_id, group_id):
        pass


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


# --- door 3: who may read, or rewrite, a group's history ------------------
#
# The three GROUP_HISTORY_* handlers and the group branch of the 1:1 request
# handler were written for the v0.20.0 hash-based sync and never got the check
# GROUP_SYNC has had since `3e49b044`. Measured live on 2026-08-28: the node
# that had removed us from a group still answered our status with its 28
# messages, and we handed back our 21.

from dpc_client_core.message_handlers.group_handler import (  # noqa: E402
    GroupAccessDeniedHandler,
    GroupHistoryRequestHandler,
    GroupHistoryResponseHandler,
    GroupHistoryStatusHandler,
)
from dpc_client_core.message_handlers.chat_history_handlers import (  # noqa: E402
    RequestChatHistoryHandler,
)


def _commands(service):
    return [m["command"] for _, m in service.p2p_manager.sent]


@pytest.fixture
def served(group_manager):
    """A service holding one group's history, with MEMBER in it and OUTSIDER not."""
    manager, group = group_manager
    service = _Service(manager)
    monitor = _Monitor()
    monitor.export_history = lambda authors=None: [{"id": "m-1", "content": "ours"}]
    monitor.merge_history = lambda messages: len(messages)
    monitor.compute_history_hash = lambda: "sha256:ours"
    monitor.history_digest = lambda: {"authors": {MEMBER: "digest"}}
    service.conversation_monitors[group.group_id] = monitor
    service._get_or_create_conversation_monitor = lambda gid: monitor
    return service, group, monitor


@pytest.mark.asyncio
async def test_an_outsider_asking_for_history_is_refused_and_gets_none(served):
    service, group, _ = served

    await GroupHistoryRequestHandler(service).handle(
        OUTSIDER, {"group_id": group.group_id}
    )

    assert _commands(service) == ["GROUP_ACCESS_DENIED"]


@pytest.mark.asyncio
async def test_an_outsider_learns_nothing_from_a_status(served):
    """The reply would otherwise disclose our count and per-author digest."""
    service, group, _ = served

    await GroupHistoryStatusHandler(service).handle(
        OUTSIDER,
        {"group_id": group.group_id, "history_hash": "sha256:theirs",
         "message_count": 99, "is_reply": False},
    )

    assert _commands(service) == ["GROUP_ACCESS_DENIED"]


@pytest.mark.asyncio
async def test_an_unknown_group_answers_exactly_like_a_refused_one(served):
    """Otherwise the difference is an oracle: walk ids, learn what we hold."""
    service, group, _ = served

    await GroupHistoryRequestHandler(service).handle(
        OUTSIDER, {"group_id": group.group_id}
    )
    refused = [m for _, m in service.p2p_manager.sent][0]
    service.p2p_manager.sent.clear()

    await GroupHistoryRequestHandler(service).handle(
        OUTSIDER, {"group_id": "group-000000000000"}
    )
    unknown = [m for _, m in service.p2p_manager.sent][0]

    assert refused["payload"]["reason"] == unknown["payload"]["reason"]
    assert set(refused["payload"]) == set(unknown["payload"])


@pytest.mark.asyncio
async def test_an_outsiders_history_is_not_merged(served):
    service, group, monitor = served
    merged = []
    monitor.merge_history = lambda messages: merged.extend(messages) or len(messages)

    await GroupHistoryResponseHandler(service).handle(
        OUTSIDER,
        {"group_id": group.group_id, "history": [{"id": "theirs", "content": "planted"}]},
    )

    assert merged == [], "an unasked-for history from a stranger must not land"


@pytest.mark.asyncio
async def test_the_one_to_one_door_is_gated_when_the_id_names_a_group(served):
    """Same export, older command — the door nobody thought to close."""
    service, group, _ = served

    await RequestChatHistoryHandler(service).handle(
        OUTSIDER, {"conversation_id": group.group_id, "request_id": "r-1"}
    )

    assert _commands(service) == ["GROUP_ACCESS_DENIED"]


@pytest.mark.asyncio
async def test_a_member_is_still_served(served):
    """The gate must not break the flow it protects."""
    service, group, _ = served

    await GroupHistoryRequestHandler(service).handle(
        MEMBER, {"group_id": group.group_id}
    )

    assert _commands(service) == ["GROUP_HISTORY_RESPONSE"]


# --- and the news the refusal carries ------------------------------------


@pytest.mark.asyncio
async def test_a_refusal_stops_us_asking_that_peer_again(served):
    service, group, _ = served
    noted = []
    service.note_group_access_denied = lambda peer, gid: noted.append((peer, gid))

    await GroupAccessDeniedHandler(service).handle(
        MEMBER, {"group_id": group.group_id, "reason": "not_a_member"}
    )

    assert noted == [(MEMBER, group.group_id)]
    assert [name for name, _ in service.local_api.events] == ["group_access_denied"]


@pytest.mark.asyncio
async def test_a_refusal_erases_nothing(served):
    """A peer saying no is not authority over our roster."""
    service, group, _ = served

    await GroupAccessDeniedHandler(service).handle(
        MEMBER, {"group_id": group.group_id, "reason": "not_a_member"}
    )

    assert service.group_manager.get_group(group.group_id) is not None


# --- and the news itself, which is what makes the gate humane -------------
#
# Removal built its announcement *after* emptying the roster of the node being
# removed, so the only node that needed it was the only one excluded — the
# comment above that call said "including the removed one" and had been false
# since it was written. Paired with the gate on purpose: a gate without this
# leaves a removed node with silent refusals instead of news, and this without a
# gate is news through a door still open.


@pytest.mark.asyncio
async def test_the_removed_node_is_told_while_it_is_connected(group_manager):
    from dpc_client_core.service import CoreService

    manager, group = group_manager
    service = CoreService.__new__(CoreService)
    service.group_manager = manager
    service.p2p_manager = _P2P()
    service.p2p_manager.peers = {MEMBER: object()}
    service.local_api = _Api()

    result = await service.remove_group_member(group.group_id, MEMBER)

    assert result["status"] == "success"
    told = [m for node, m in service.p2p_manager.sent if node == MEMBER]
    assert [m["command"] for m in told] == ["GROUP_SYNC"]
    assert MEMBER not in told[0]["payload"]["members"], (
        "the roster it is handed is the one that leaves it out"
    )


@pytest.mark.asyncio
async def test_a_node_removed_while_offline_is_not_left_in_the_dark_silently(group_manager):
    """Offline removal is a catch-up channel we do not have yet — say so in the
    log rather than pretend the message was delivered."""
    manager, group = group_manager
    from dpc_client_core.service import CoreService

    service = CoreService.__new__(CoreService)
    service.group_manager = manager
    service.p2p_manager = _P2P()          # nobody connected
    service.local_api = _Api()

    result = await service.remove_group_member(group.group_id, MEMBER)

    assert result["status"] == "success"
    assert service.p2p_manager.sent == []


def test_a_removed_node_stops_advertising_the_group(tmp_path):
    """Ark's idempotence requirement: once the news lands, the asking stops.

    The creator stays in the group, so a loop keyed only on *their* membership
    would go on offering a group we are no longer in — one refusal per reconnect,
    for ever.
    """
    manager = GroupManager(dpc_home=tmp_path, node_id=SELF)
    group = manager.create_group("work", "topic", [MEMBER])
    assert group.group_id in [g.group_id for g in manager.get_groups_for_peer(MEMBER)]

    removed = group.to_dict()
    removed["version"] = group.version + 1
    removed["members"] = [MEMBER]          # the roster that leaves SELF out
    manager.apply_sync(removed)

    assert manager.get_groups_for_peer(MEMBER) == []


# --- door 4: an answer is only an answer if we asked ----------------------
#
# `3e49b044` gave CHAT_HISTORY_RESPONSE a claim against a recorded question:
# «Unclaimed, it lets any connected peer overwrite a conversation it was never
# part of». The group twin arrived with the v0.20.0 hash sync and got none, so a
# member could push a history nobody asked for — and a member is exactly who is
# in a position to.


@pytest.mark.asyncio
async def test_an_unasked_group_history_is_discarded_even_from_a_member(served):
    service, group, monitor = served
    merged = []
    monitor.merge_history = lambda messages: merged.extend(messages) or len(messages)

    await GroupHistoryResponseHandler(service).handle(
        MEMBER,
        {"group_id": group.group_id, "history": [{"id": "x", "content": "unasked"}],
         "request_id": "never-issued"},
    )

    assert merged == []


@pytest.mark.asyncio
async def test_the_answer_to_our_own_question_is_merged(served):
    service, group, monitor = served
    merged = []
    monitor.merge_history = lambda messages: merged.extend(messages) or len(messages)
    service.history_requests.note(MEMBER, group.group_id, "r-1")

    await GroupHistoryResponseHandler(service).handle(
        MEMBER,
        {"group_id": group.group_id, "history": [{"id": "x", "content": "asked for"}],
         "request_id": "r-1"},
    )

    assert [m["id"] for m in merged] == ["x"]


@pytest.mark.asyncio
async def test_one_question_earns_one_answer(served):
    """A peer cannot keep rewriting by replaying the id we handed it."""
    service, group, monitor = served
    merged = []
    monitor.merge_history = lambda messages: merged.extend(messages) or len(messages)
    service.history_requests.note(MEMBER, group.group_id, "r-1")
    reply = {"group_id": group.group_id, "history": [{"id": "x", "content": "again"}],
             "request_id": "r-1"}

    await GroupHistoryResponseHandler(service).handle(MEMBER, reply)
    await GroupHistoryResponseHandler(service).handle(MEMBER, reply)

    assert len(merged) == 1


@pytest.mark.asyncio
async def test_a_peer_on_the_older_build_is_still_understood(served):
    """It answers without echoing the id; what must hold is that we asked."""
    service, group, monitor = served
    merged = []
    monitor.merge_history = lambda messages: merged.extend(messages) or len(messages)
    service.history_requests.note(MEMBER, group.group_id, "r-1")

    await GroupHistoryResponseHandler(service).handle(
        MEMBER,
        {"group_id": group.group_id, "history": [{"id": "x", "content": "no id echoed"}]},
    )

    assert [m["id"] for m in merged] == ["x"]


@pytest.mark.asyncio
async def test_the_request_we_send_carries_an_id_and_is_recorded(served):
    """The other half of the claim: a question nobody wrote down cannot be met."""
    service, group, monitor = served
    monitor.history_digest = lambda: {"authors": {MEMBER: "ours"}}

    await GroupHistoryStatusHandler(service).handle(
        MEMBER,
        {"group_id": group.group_id, "history_hash": "sha256:theirs",
         "message_count": 2, "history_digest": {"authors": {MEMBER: "theirs"}},
         "is_reply": True},
    )

    asked = [m for _, m in service.p2p_manager.sent if m["command"] == "GROUP_HISTORY_REQUEST"]
    assert len(asked) == 1
    request_id = asked[0]["payload"]["request_id"]
    assert request_id
    assert service.history_requests.claim(MEMBER, group.group_id, request_id)
