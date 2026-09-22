"""
A GOSSIP_MESSAGE is believed to come from its `source` only when it proves it.

Board: AN-INBOUND-GOSSIP-FRAME-IS-FORWARDED-ON-AN-UNSIGNED-SOURCE-SO-ANY-PEER-
CAN-SPEAK-AS-ANYONE-AND-SPEND-OUR-FANOUT. Until 2026-09-23 the manager read
`source` off the envelope, forwarded on it and handed the decrypted payload to
the message router as that node - so any connected peer could deliver any DPTP
command as any node id. Encryption to the destination proves nothing about the
author: anyone can encrypt to our public key.

Now the source signs at origin and carries its certificate; every receiver
re-derives the node id from that certificate, requires it to equal `source`,
and checks the signature before it delivers, stores, merges or forwards.
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_protocol.message_signing import GOSSIP_PREIMAGE_VERSION
from dpc_client_core.managers.gossip_manager import GossipManager, gossip_message_frame
from dpc_client_core.message_handlers.gossip_handler import GossipMessageHandler
from dpc_client_core.models.gossip_message import GossipMessage


class Identity:
    """A node's key, the node id its key hashes to, and its certificate."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.node_id = generate_node_id(self.key.public_key())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.node_id)])
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .sign(self.key, hashes.SHA256())
        )
        self.cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        self.signer = CommitSigner(self.node_id, self.key)


@pytest.fixture(scope="module")
def ids():
    return {name: Identity() for name in ("alice", "bob", "charlie", "dave", "mallory")}


def peer(node_id: str) -> Mock:
    p = Mock()
    p.node_id = node_id
    p.send = AsyncMock()
    return p


def node(identity: Identity, connected=()) -> GossipManager:
    p2p = Mock()
    p2p.get_connected_peers = Mock(return_value=list(connected))
    p2p.peers = {p.node_id: p for p in connected}
    router = Mock()
    router.route_message = AsyncMock()
    manager = GossipManager(p2p, identity.node_id, message_router=router)
    # Encryption is covered by test_gossip_encryption.py; the blob is opaque here.
    manager._encrypt_payload = AsyncMock(return_value="opaque-blob")
    manager._decrypt_payload = AsyncMock(return_value={"command": "SEND_TEXT", "text": "hi"})
    manager._origin_identity = lambda: (identity.signer, identity.cert_pem)
    return manager


def handler_for(manager: GossipManager) -> GossipMessageHandler:
    service = Mock()
    service.gossip_manager = manager
    return GossipMessageHandler(service)


def signed(msg: GossipMessage, by: Identity, cert_of: Identity = None) -> GossipMessage:
    """Sign ``msg`` with ``by``'s key, carrying ``cert_of``'s certificate."""
    msg.gossip_preimage_version = GOSSIP_PREIMAGE_VERSION
    msg.cert_pem = (cert_of or by).cert_pem
    msg.signature = by.signer.sign_commit(msg.origin_hash())
    return msg


def new_message(source: str, destination: str) -> GossipMessage:
    return GossipMessage.create(
        source=source, destination=destination, payload={"encrypted": "opaque-blob"},
        max_hops=5, ttl=86400, priority="normal", vector_clock={source: 1},
    )


def refusals(manager: GossipManager) -> dict:
    return {k: v for k, v in manager.stats.items() if k.endswith("_frames_refused")}


# --- refusals --------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsigned_frame_claiming_another_source_is_neither_delivered_nor_forwarded(ids, caplog):
    """Mallory hands Bob, and a relay Charlie, a frame that says it is from Alice."""
    alice, bob, charlie, mallory = ids["alice"], ids["bob"], ids["charlie"], ids["mallory"]
    dave_peer = peer(ids["dave"].node_id)
    bob_node = node(bob)
    charlie_node = node(charlie, [dave_peer])

    forged = new_message(alice.node_id, bob.node_id)
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.managers.gossip_manager"):
        await handler_for(bob_node).handle(mallory.node_id, gossip_message_frame(forged)["payload"])
        await handler_for(charlie_node).handle(mallory.node_id, gossip_message_frame(forged)["payload"])

    bob_node.message_router.route_message.assert_not_awaited()
    bob_node._decrypt_payload.assert_not_awaited()
    dave_peer.send.assert_not_awaited()
    for n in (bob_node, charlie_node):
        assert n.stats["unsigned_frames_refused"] == 1
        assert forged.id not in n.messages and forged.id not in n.seen_messages
        assert n.stats["messages_forwarded"] == 0
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(alice.node_id[:20] in w and mallory.node_id[:20] in w for w in warnings)


@pytest.mark.asyncio
async def test_frame_signed_by_one_node_claiming_another_is_refused(ids):
    """Mallory signs with her own key and carries her own certificate, source=Alice."""
    alice, bob, mallory = ids["alice"], ids["bob"], ids["mallory"]
    bob_node = node(bob)

    forged = signed(new_message(alice.node_id, bob.node_id), by=mallory)
    await handler_for(bob_node).handle(mallory.node_id, gossip_message_frame(forged)["payload"])

    bob_node.message_router.route_message.assert_not_awaited()
    assert refusals(bob_node)["source_mismatch_frames_refused"] == 1
    assert forged.id not in bob_node.seen_messages


@pytest.mark.asyncio
async def test_frame_carrying_the_victims_certificate_but_signed_by_another_key_is_refused(ids):
    """Alice's certificate is public; carrying it without her key proves nothing."""
    alice, bob, mallory = ids["alice"], ids["bob"], ids["mallory"]
    bob_node = node(bob)

    forged = signed(new_message(alice.node_id, bob.node_id), by=mallory, cert_of=alice)
    await handler_for(bob_node).handle(mallory.node_id, gossip_message_frame(forged)["payload"])

    bob_node.message_router.route_message.assert_not_awaited()
    assert refusals(bob_node)["bad_signature_frames_refused"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("id", "msg-tampered-" + "0" * 20),
    ("payload", {"encrypted": "other-blob"}),
    ("destination", "dpc-node-" + "0" * 32),
    ("max_hops", 4),
    ("created_at", 0.0),
    ("ttl", 10 ** 9),
    ("priority", "high"),
    ("vector_clock", {"dpc-node-x": 99}),
])
async def test_signed_frame_altered_in_transit_is_refused(ids, field, value):
    alice, bob, charlie = ids["alice"], ids["bob"], ids["charlie"]
    dave_peer = peer(ids["dave"].node_id)
    bob_node = node(bob)
    charlie_node = node(charlie, [dave_peer])

    frame = gossip_message_frame(signed(new_message(alice.node_id, bob.node_id), by=alice))
    frame["payload"]["gossip_message"][field] = value
    await handler_for(bob_node).handle(charlie.node_id, frame["payload"])
    await handler_for(charlie_node).handle(alice.node_id, frame["payload"])

    bob_node.message_router.route_message.assert_not_awaited()
    dave_peer.send.assert_not_awaited()
    assert bob_node.stats["bad_signature_frames_refused"] == 1
    assert charlie_node.stats["bad_signature_frames_refused"] == 1


def test_frame_without_signature_fields_still_deserializes_and_round_trips(ids):
    alice, bob = ids["alice"], ids["bob"]
    bare = new_message(alice.node_id, bob.node_id).to_dict()
    for k in ("signature", "cert_pem", "gossip_preimage_version"):
        bare.pop(k)
    msg = GossipMessage.from_dict(bare)
    assert msg.signature is None and msg.cert_pem is None

    full = signed(new_message(alice.node_id, bob.node_id), by=alice)
    again = GossipMessage.from_dict(full.to_dict())
    assert again.to_dict() == full.to_dict()


# --- acceptance ------------------------------------------------------------


@pytest.mark.asyncio
async def test_signed_frame_is_forwarded_and_delivered_with_its_source(ids):
    alice, bob, charlie = ids["alice"], ids["bob"], ids["charlie"]
    dave_peer = peer(ids["dave"].node_id)
    bob_node = node(bob)
    charlie_node = node(charlie, [dave_peer])

    frame = gossip_message_frame(signed(new_message(alice.node_id, bob.node_id), by=alice))
    await handler_for(charlie_node).handle(alice.node_id, frame["payload"])
    await handler_for(bob_node).handle(charlie.node_id, frame["payload"])

    dave_peer.send.assert_awaited_once()
    bob_node.message_router.route_message.assert_awaited_once()
    assert bob_node.message_router.route_message.await_args.args[0] == alice.node_id
    assert refusals(bob_node) == {k: 0 for k in refusals(bob_node)}


@pytest.mark.asyncio
async def test_relays_rewriting_hops_do_not_break_verification(ids):
    """alice -> charlie -> dave -> bob: two relays increment hops/already_forwarded."""
    alice, bob, charlie, dave = ids["alice"], ids["bob"], ids["charlie"], ids["dave"]
    charlie_peer, dave_peer, bob_peer = peer(charlie.node_id), peer(dave.node_id), peer(bob.node_id)
    alice_node = node(alice, [charlie_peer])
    charlie_node = node(charlie, [dave_peer])
    dave_node = node(dave, [bob_peer])
    bob_node = node(bob)

    await alice_node.send_gossip(bob.node_id, {"command": "SEND_TEXT", "text": "hop"})
    await handler_for(charlie_node).handle(alice.node_id, charlie_peer.send.await_args.args[0]["payload"])
    await handler_for(dave_node).handle(charlie.node_id, dave_peer.send.await_args.args[0]["payload"])
    last = bob_peer.send.await_args.args[0]["payload"]
    assert last["gossip_message"]["hops"] == 3
    assert last["gossip_message"]["already_forwarded"] == [alice.node_id, charlie.node_id, dave.node_id]
    await handler_for(bob_node).handle(dave.node_id, last)

    bob_node.message_router.route_message.assert_awaited_once()
    assert bob_node.message_router.route_message.await_args.args[0] == alice.node_id
    for n in (charlie_node, dave_node, bob_node):
        assert sum(refusals(n).values()) == 0


@pytest.mark.asyncio
async def test_send_gossip_emits_a_frame_the_receiver_accepts(ids):
    alice, bob = ids["alice"], ids["bob"]
    bob_peer = peer(bob.node_id)
    alice_node = node(alice, [bob_peer])
    bob_node = node(bob)

    msg_id = await alice_node.send_gossip(bob.node_id, {"command": "SEND_TEXT", "text": "hi"})
    frame = bob_peer.send.await_args.args[0]
    assert frame["payload"]["gossip_message"]["signature"]
    assert frame["payload"]["gossip_message"]["cert_pem"] == alice.cert_pem
    await handler_for(bob_node).handle(alice.node_id, frame["payload"])

    bob_node.message_router.route_message.assert_awaited_once_with(
        alice.node_id, {"command": "SEND_TEXT", "text": "hi"}
    )
    assert msg_id in bob_node.seen_messages


@pytest.mark.asyncio
async def test_delivery_is_attributed_to_the_certificate_not_a_later_mutated_source(ids):
    """_deliver_message must use the id proven_origin() derived, not msg.source
    re-read - falsified by reverting to msg.source: a mutation between the
    origin check and delivery would then reach the router under the wrong id."""
    alice, bob = ids["alice"], ids["bob"]
    bob_node = node(bob)
    msg = signed(new_message(alice.node_id, bob.node_id), by=alice)
    msg.source = "dpc-node-" + "9" * 32  # mutated after the check, before delivery

    await bob_node._deliver_message(msg, alice.node_id)

    bob_node.message_router.route_message.assert_awaited_once_with(
        alice.node_id, {"command": "SEND_TEXT", "text": "hi"}
    )


@pytest.mark.asyncio
async def test_already_forwarded_naming_the_recipient_does_not_stop_sync_delivery(ids):
    """already_forwarded is a fan-out hint, unsigned - a relay padding it with
    bob's id only suppresses _forward_message's fanout, not the anti-entropy
    pull, which resends by message_ids regardless of the list."""
    alice, bob = ids["alice"], ids["bob"]
    msg = signed(new_message(alice.node_id, bob.node_id), by=alice)
    msg.already_forwarded.append(bob.node_id)

    charlie_relay = node(ids["charlie"])
    charlie_relay.messages[msg.id] = msg
    bob_peer = peer(bob.node_id)
    charlie_relay.p2p_manager.peers = {bob.node_id: bob_peer}

    await charlie_relay.handle_gossip_sync(peer_id=bob.node_id, peer_clock_dict={}, peer_message_ids=[])
    bob_peer.send.assert_awaited_once()

    bob_node = node(bob)
    await handler_for(bob_node).handle(charlie_relay.node_id, bob_peer.send.await_args.args[0]["payload"])
    bob_node.message_router.route_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_replayed_signed_frame_is_delivered_once(ids):
    alice, bob = ids["alice"], ids["bob"]
    bob_node = node(bob)
    frame = gossip_message_frame(signed(new_message(alice.node_id, bob.node_id), by=alice))
    await handler_for(bob_node).handle(alice.node_id, frame["payload"])
    await handler_for(bob_node).handle(ids["mallory"].node_id, frame["payload"])
    bob_node.message_router.route_message.assert_awaited_once()


# --- the key on disk -------------------------------------------------------


def _write_identity(home, identity: Identity):
    dpc = home / ".dpc"
    dpc.mkdir(parents=True, exist_ok=True)
    (dpc / "node.key").write_bytes(identity.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    (dpc / "node.id").write_text(identity.node_id)
    (dpc / "node.crt").write_text(identity.cert_pem)


@pytest.mark.asyncio
async def test_send_gossip_signs_with_the_key_on_disk(ids, tmp_path, monkeypatch):
    import pathlib
    alice, bob = ids["alice"], ids["bob"]
    _write_identity(tmp_path, alice)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    bob_peer = peer(bob.node_id)
    alice_node = node(alice, [bob_peer])
    del alice_node._origin_identity  # the real loader
    bob_node = node(bob)

    await alice_node.send_gossip(bob.node_id, {"command": "SEND_TEXT", "text": "disk"})
    await handler_for(bob_node).handle(alice.node_id, bob_peer.send.await_args.args[0]["payload"])

    bob_node.message_router.route_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_gossip_without_a_key_refuses_rather_than_sending_unsigned(ids, tmp_path, monkeypatch):
    import pathlib
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    bob_peer = peer(ids["bob"].node_id)
    alice_node = node(ids["alice"], [bob_peer])
    del alice_node._origin_identity

    with pytest.raises(ValueError):
        await alice_node.send_gossip(ids["bob"].node_id, {"command": "SEND_TEXT"})
    bob_peer.send.assert_not_awaited()
    assert alice_node.messages == {}
