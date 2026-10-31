"""A volunteer relay turns two registrations that name each other into one session.

`peer_to_session` used to be written only inside the branch that tests it, so no
registration ever found its target there and every peer was told RELAY_WAITING
until the dialling side timed out. Driven through the real
`RelayRegisterHandler`, with connections that record what they were sent.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers.relay_manager import RelayManager
from dpc_client_core.message_handlers.relay_register_handler import RelayRegisterHandler

SELF = "dpc-node-" + "a" * 32
ALICE = "dpc-node-" + "1" * 32
BOB = "dpc-node-" + "2" * 32
CAROL = "dpc-node-" + "3" * 32


class _Connection:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    def commands(self):
        return [m["command"] for m in self.sent]


class _Firewall:
    def is_blocked(self, node_id):
        return False


def _setup():
    from dpc_client_core.p2p_manager import P2PManager

    p2p = P2PManager.__new__(P2PManager)  # no __init__: it reads ~/.dpc
    p2p.node_id = SELF
    p2p.firewall = _Firewall()
    conns = {n: _Connection() for n in (ALICE, BOB, CAROL)}
    p2p.peers = dict(conns)
    relay = RelayManager(SimpleNamespace(node_id=SELF), p2p_manager=p2p, volunteer=True)
    service = SimpleNamespace(p2p_manager=p2p, relay_manager=relay)
    return RelayRegisterHandler(service), relay, conns


async def _pair(handler):
    await handler.handle(ALICE, {"peer_id": BOB})
    await handler.handle(BOB, {"peer_id": ALICE})


@pytest.mark.asyncio
async def test_the_first_peer_waits_and_the_second_completes_the_pair():
    handler, relay, conns = _setup()

    await handler.handle(ALICE, {"peer_id": BOB})
    assert conns[ALICE].commands() == ["RELAY_WAITING"]
    assert relay.sessions == {}

    await handler.handle(BOB, {"peer_id": ALICE})
    assert conns[BOB].commands() == ["RELAY_READY"]
    assert conns[ALICE].commands() == ["RELAY_WAITING", "RELAY_READY"]


@pytest.mark.asyncio
async def test_each_ready_names_the_other_peer_and_both_map_to_one_session():
    handler, relay, conns = _setup()
    await _pair(handler)

    to_bob = conns[BOB].sent[-1]["payload"]
    to_alice = conns[ALICE].sent[-1]["payload"]
    assert to_bob["peer_id"] == ALICE and to_alice["peer_id"] == BOB
    assert to_bob["session_id"] == to_alice["session_id"]
    assert relay.peer_to_session == {ALICE: to_bob["session_id"], BOB: to_bob["session_id"]}
    assert list(relay.sessions) == [to_bob["session_id"]]
    assert relay.peer_waiting_for == {}


@pytest.mark.asyncio
async def test_after_pairing_a_message_is_forwarded_to_the_other_peer():
    handler, relay, conns = _setup()
    await _pair(handler)
    session_id = relay.peer_to_session[ALICE]

    assert await relay.handle_relay_message(ALICE, BOB, b"opaque-blob") is True

    forwarded = conns[BOB].sent[-1]
    assert forwarded == {
        "command": "RELAY_MESSAGE",
        "payload": {"from": ALICE, "to": BOB, "session_id": session_id, "data": "opaque-blob"},
    }
    assert relay.sessions[session_id].messages_relayed == 1


@pytest.mark.asyncio
async def test_a_registration_for_someone_else_does_not_pair():
    handler, relay, conns = _setup()

    await handler.handle(ALICE, {"peer_id": BOB})
    await handler.handle(CAROL, {"peer_id": ALICE})  # Alice waits for Bob, not Carol

    assert relay.sessions == {} and relay.peer_to_session == {}
    assert conns[CAROL].commands() == ["RELAY_WAITING"]
    assert conns[ALICE].commands() == ["RELAY_WAITING"]
    assert relay.peer_waiting_for == {ALICE: BOB, CAROL: ALICE}


@pytest.mark.asyncio
async def test_a_second_registration_by_the_same_peer_replaces_its_waiting_entry():
    handler, relay, conns = _setup()

    await handler.handle(ALICE, {"peer_id": BOB})
    await handler.handle(ALICE, {"peer_id": CAROL})
    assert relay.peer_waiting_for == {ALICE: CAROL}

    await handler.handle(BOB, {"peer_id": ALICE})  # Alice no longer waits for Bob
    assert relay.sessions == {}


@pytest.mark.asyncio
async def test_a_waiting_peer_that_disconnects_is_forgotten_and_not_paired_with():
    handler, relay, conns = _setup()
    await handler.handle(ALICE, {"peer_id": BOB})

    relay.forget_peer(ALICE)  # what CoreService._handle_peer_disconnected calls
    assert relay.peer_waiting_for == {} and ALICE not in relay.peer_connections

    await handler.handle(BOB, {"peer_id": ALICE})
    assert conns[BOB].commands() == ["RELAY_WAITING"]
    assert relay.sessions == {}
    assert conns[ALICE].commands() == ["RELAY_WAITING"]  # nothing sent to the dead link


@pytest.mark.asyncio
async def test_a_lost_peer_is_forgotten_by_the_service_disconnect_callback():
    from dpc_client_core.service import CoreService

    handler, relay, conns = _setup()
    await handler.handle(ALICE, {"peer_id": BOB})

    service = CoreService.__new__(CoreService)
    service.relay_manager = relay
    service._history_requested_peers = set()
    service.history_requests = SimpleNamespace(forget_peer=lambda peer: None)
    service.file_transfer_manager = SimpleNamespace(active_transfers={})
    service.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    service.connection_orchestrator = None  # no redial: the relay step is what is under test

    await service._handle_peer_disconnected(ALICE)

    assert relay.peer_waiting_for == {} and ALICE not in relay.peer_connections
