"""
A GOSSIP_MESSAGE frame leaves every sender in one shape and the handler reads it.

Board: A-GOSSIP-FRAME-LEAVES-FLAT-FROM-FAN-OUT-AND-WRAPPED-FROM-ANTI-ENTROPY-AND-
THE-HANDLER-READS-ONLY-THE-WRAPPED-ONE. Until 2026-09-14 fan-out and every
multi-hop forward sent the message flat in the payload while the handler read
only ``payload["gossip_message"]`` (the shape DPTP §3.10 states), so the sixth
tier delivered through anti-entropy alone. These tests push a frame built by
each real sender path through ``GossipMessageHandler.handle`` and require it
to reach ``GossipManager.handle_gossip_message`` on the far side.
"""

import logging
from unittest.mock import AsyncMock, Mock

import pytest

from dpc_client_core.managers import gossip_manager as gm
from dpc_client_core.managers.gossip_manager import GossipManager
from dpc_client_core.message_handlers.gossip_handler import GossipMessageHandler
from dpc_client_core.models.gossip_message import GossipMessage
from tests.test_gossip_signed_source import Identity, signed

# Real keys: a receiver refuses a frame whose source did not sign it.
IDENTITIES = {i.node_id: i for i in (Identity() for _ in range(4))}
ALICE, BOB, CHARLIE, DAVE = IDENTITIES


def capturing_peer(node_id: str) -> Mock:
    """A connected peer whose ``send`` records the frame it was given."""
    peer = Mock()
    peer.node_id = node_id
    peer.send = AsyncMock()
    return peer


def node(node_id: str, connected_peers=()) -> GossipManager:
    """A GossipManager whose P2P layer sees exactly ``connected_peers``."""
    p2p = Mock()
    p2p.get_connected_peers = Mock(return_value=list(connected_peers))
    p2p.peers = {p.node_id: p for p in connected_peers}
    manager = GossipManager(p2p, node_id)
    # Encryption is covered by test_gossip_encryption.py; here the payload is opaque.
    manager._encrypt_payload = AsyncMock(return_value="opaque-blob")
    identity = IDENTITIES[node_id]
    manager._origin_identity = lambda: (identity.signer, identity.cert_pem)
    return manager


def receiving_handler(manager: GossipManager) -> GossipMessageHandler:
    """The handler as the message router builds it, with the manager's real
    handle_gossip_message left in place but observable."""
    manager.handle_gossip_message = AsyncMock(wraps=manager.handle_gossip_message)
    service = Mock()
    service.gossip_manager = manager
    return GossipMessageHandler(service)


def sent_frame(peer: Mock) -> dict:
    peer.send.assert_awaited_once()
    return peer.send.await_args.args[0]


@pytest.mark.asyncio
async def test_fan_out_frame_reaches_the_receivers_manager():
    """(a) The frame send_gossip fans out is the frame the handler accepts."""
    charlie_peer = capturing_peer(CHARLIE)
    alice = node(ALICE, [charlie_peer])
    charlie = node(CHARLIE)
    handler = receiving_handler(charlie)

    msg_id = await alice.send_gossip(BOB, {"command": "SEND_TEXT", "text": "hi"})

    frame = sent_frame(charlie_peer)
    assert frame["command"] == "GOSSIP_MESSAGE"
    await handler.handle(ALICE, frame["payload"])

    charlie.handle_gossip_message.assert_awaited_once()
    received = charlie.handle_gossip_message.await_args.args[0]
    assert isinstance(received, GossipMessage)
    assert received.id == msg_id
    assert received.destination == BOB
    assert charlie.stats["messages_received"] == 1
    assert charlie.stats["flat_frames_accepted"] == 0
    assert not [k for k, v in charlie.stats.items() if k.endswith("_frames_refused") and v]


@pytest.mark.asyncio
async def test_second_hop_forward_frame_reaches_the_next_manager():
    """(b) A frame forwarded by an intermediate hop is read by the next hop."""
    charlie_peer = capturing_peer(CHARLIE)
    dave_peer = capturing_peer(DAVE)
    alice = node(ALICE, [charlie_peer])
    charlie = node(CHARLIE, [dave_peer])
    dave = node(DAVE)
    charlie_handler = receiving_handler(charlie)
    dave_handler = receiving_handler(dave)

    msg_id = await alice.send_gossip(BOB, {"command": "SEND_TEXT", "text": "hop"})

    # Hop 1: alice -> charlie, who stores and forwards because bob is not him.
    await charlie_handler.handle(ALICE, sent_frame(charlie_peer)["payload"])
    charlie.handle_gossip_message.assert_awaited_once()

    # Hop 2: charlie -> dave, the frame _forward_message built on the way out.
    second_hop = sent_frame(dave_peer)
    assert second_hop["command"] == "GOSSIP_MESSAGE"
    await dave_handler.handle(CHARLIE, second_hop["payload"])

    dave.handle_gossip_message.assert_awaited_once()
    received = dave.handle_gossip_message.await_args.args[0]
    assert received.id == msg_id
    assert received.hops == 2
    assert received.already_forwarded == [ALICE, CHARLIE]
    assert dave.stats["flat_frames_accepted"] == 0
    assert not [k for k, v in dave.stats.items() if k.endswith("_frames_refused") and v]


@pytest.mark.asyncio
async def test_anti_entropy_resend_still_reaches_the_receiver(monkeypatch):
    """(c) The anti-entropy resend goes through the one helper and is read."""
    charlie_peer = capturing_peer(CHARLIE)
    alice = node(ALICE, [charlie_peer])
    charlie = node(CHARLIE)
    handler = receiving_handler(charlie)

    # The helper must be the only place a GOSSIP_MESSAGE frame is shaped, so
    # every sender is watched through it: remove it from a sender and this spy
    # is never called for that sender.
    real_frame = gm.gossip_message_frame
    spy = Mock(wraps=real_frame)
    monkeypatch.setattr(gm, "gossip_message_frame", spy)

    msg = signed(GossipMessage.create(
        source=ALICE, destination=BOB, payload={"encrypted": "opaque-blob"},
        max_hops=5, ttl=86400, priority="normal", vector_clock={ALICE: 1},
    ), by=IDENTITIES[ALICE])
    alice.messages[msg.id] = msg

    await alice.handle_gossip_sync(peer_id=CHARLIE, peer_clock_dict={}, peer_message_ids=[])

    spy.assert_called_once_with(msg)
    frame = sent_frame(charlie_peer)
    assert frame == real_frame(msg)
    await handler.handle(ALICE, frame["payload"])

    charlie.handle_gossip_message.assert_awaited_once()
    assert charlie.handle_gossip_message.await_args.args[0].id == msg.id
    assert charlie.stats["flat_frames_accepted"] == 0
    assert not [k for k, v in charlie.stats.items() if k.endswith("_frames_refused") and v]


@pytest.mark.asyncio
async def test_flat_frame_from_an_older_node_is_accepted_with_a_counted_warning(caplog):
    """(d) A pre-fix peer still fans out flat; for one release it is read, not lost."""
    charlie = node(CHARLIE)
    handler = receiving_handler(charlie)
    msg = signed(GossipMessage.create(
        source=ALICE, destination=BOB, payload={"encrypted": "opaque-blob"},
        max_hops=5, ttl=86400, priority="normal", vector_clock={ALICE: 1},
    ), by=IDENTITIES[ALICE])

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.message_handlers"):
        await handler.handle(ALICE, msg.to_dict())  # flat: message keys at the top

    charlie.handle_gossip_message.assert_awaited_once()
    assert charlie.handle_gossip_message.await_args.args[0].id == msg.id
    assert charlie.stats["flat_frames_accepted"] == 1
    assert not [k for k, v in charlie.stats.items() if k.endswith("_frames_refused") and v]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "flat" in warnings[0].getMessage()
    assert ALICE[:20] in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_frame_with_neither_shape_is_dropped_with_the_old_warning(caplog):
    """A payload that is neither wrapped nor flat is still refused, and the
    drop line keeps its wording so the log grep of 2026-09-13 stays comparable."""
    charlie = node(CHARLIE)
    handler = receiving_handler(charlie)

    with caplog.at_level(logging.WARNING, logger="dpc_client_core.message_handlers"):
        await handler.handle(ALICE, {"vector_clock": {}, "message_ids": []})

    charlie.handle_gossip_message.assert_not_awaited()
    assert charlie.stats["flat_frames_accepted"] == 0
    assert not [k for k, v in charlie.stats.items() if k.endswith("_frames_refused") and v]
    assert any("missing 'gossip_message'" in r.getMessage() for r in caplog.records)
