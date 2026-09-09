"""The port a dial succeeded on has to survive the peer-list callback.

`connect_directly` records the endpoint it actually reached — ip *and* port —
and `_reconnect_to_peer` reads that pair back to redial. Between the two,
`CoreService.on_peer_list_change` rewrites the same row from the live
connection. It knows the peer's ip (`peername[0]`) and does not know its
listening port, and `PeerCache.add_or_update_peer` used to turn "not passed"
into 8888: the write body is `if direct_ip: ... last_direct_port = direct_port`,
with `direct_port: int = 8888` in the signature. A peer listening on 9001 was
therefore dialled once at 9001 and cached at 8888, and every later reconnect
knocked on a door nobody is behind.

Omitting a value must mean "keep what is there", never "assert the default" —
and this callback is exactly the caller that cannot know. The port cannot be
taken from `peername[1]` either: outbound that is the peer's listening port,
inbound it is an ephemeral source port, and the callback sees both kinds of
peer in one loop.
"""

import asyncio
import base64
import datetime
import os
import ssl
import types
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.protocol import read_message, write_message
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import PeerCache
from dpc_client_core.service import CoreService


def _identity(tmp_path: Path, name: str):
    """A node identity written to tmp_path — never to ~/.dpc."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / f"{name}.crt"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return node_id, cert_file, key_file


async def _peer_listening_on_a_free_port(tmp_path: Path):
    """A peer that completes the real handshake and then idles.

    Port 0 asks the OS for a free port, which is never 8888 — the point of the
    exercise is an endpoint the default cannot accidentally be right about.
    """
    node_id, cert_file, key_file = _identity(tmp_path, "peer")

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    async def handle(reader, writer):
        try:
            nonce = os.urandom(32)
            await write_message(writer, {
                "command": "HELLO_CHALLENGE",
                "payload": {"nonce": base64.b64encode(nonce).decode()},
            })
            await read_message(reader)  # HELLO
            await write_message(writer, {
                "command": "HELLO_ACK",
                "status": "OK",
                "name": "Peer",
                "node_id": node_id,
            })
            while await read_message(reader) is not None:
                pass
        except Exception:
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1], node_id


def _dialler(tmp_path: Path):
    """A P2PManager whose every file lives in tmp_path."""
    node_id, cert_file, key_file = _identity(tmp_path, "dialler")

    manager = P2PManager.__new__(P2PManager)
    manager.node_id = node_id
    manager.cert_file = cert_file
    manager.key_file = key_file
    manager.display_name = "Dialler"
    manager.settings = None
    manager.peers = {}
    manager._peer_listener_tasks = {}
    manager._intentional_disconnects = set()
    manager._hub_client_refs = {}
    manager.dht_manager = None
    manager.on_peer_list_change = None
    manager.on_message_received = None
    manager.on_peer_disconnected = None
    manager.peer_cache = PeerCache(tmp_path / "peer_cache.json")
    # The certificate store is the one thing under ~/.dpc this path would touch.
    manager._persist_peer_certificate = lambda nid, pem: True
    return manager


def _service(manager):
    """The real on_peer_list_change, with only its neighbours stood in for."""
    service = CoreService.__new__(CoreService)
    service.p2p_manager = manager
    service.peer_metadata = {}
    service._history_requested_peers = set()
    service._group_access_denied = set()
    service.conversation_monitors = {}
    service.knowledge_service = None
    service.group_manager = types.SimpleNamespace(
        get_groups_for_peer=lambda peer_id: [],
        get_deleted_group_ids=lambda: [],
    )
    service._get_or_create_conversation_monitor = lambda peer_id: None

    async def _status():
        return {}

    async def _broadcast(*args, **kwargs):
        return None

    service.get_status = _status
    service.local_api = types.SimpleNamespace(broadcast_event=_broadcast)
    return service


@pytest.mark.asyncio
async def test_the_peer_list_callback_leaves_the_dialled_port_alone(tmp_path):
    """The live path: dial a peer off 8888, then let the callback run."""
    server, port, peer_id = await _peer_listening_on_a_free_port(tmp_path)
    assert port != 8888

    manager = _dialler(tmp_path)
    try:
        await manager.connect_directly("127.0.0.1", port, peer_id, timeout=15.0)
        assert manager.peer_cache.get_peer(peer_id).last_direct_port == port

        await _service(manager).on_peer_list_change()

        assert manager.peer_cache.get_peer(peer_id).last_direct_port == port
    finally:
        for peer in list(manager.peers.values()):
            await peer.close()
        server.close()
        await server.wait_closed()


def test_a_caller_that_omits_the_port_does_not_move_it(tmp_path):
    """Cheap and direct: omission is not a value."""
    node_id = "dpc-node-" + "a" * 32
    cache = PeerCache(tmp_path / "peer_cache.json")
    cache.add_or_update_peer(node_id=node_id, direct_ip="10.0.0.5",
                             direct_port=9001, supports_direct=True)

    cache.add_or_update_peer(node_id=node_id, direct_ip="10.0.0.5",
                             supports_direct=True)

    assert cache.get_peer(node_id).last_direct_port == 9001


def test_a_caller_that_names_the_port_still_moves_it(tmp_path):
    """The other half: a caller that knows must still be able to say so."""
    node_id = "dpc-node-" + "b" * 32
    cache = PeerCache(tmp_path / "peer_cache.json")
    cache.add_or_update_peer(node_id=node_id, direct_ip="10.0.0.5",
                             direct_port=9001, supports_direct=True)

    cache.add_or_update_peer(node_id=node_id, direct_ip="10.0.0.6",
                             direct_port=8888, supports_direct=True)

    peer = cache.get_peer(node_id)
    assert (peer.last_direct_ip, peer.last_direct_port) == ("10.0.0.6", 8888)


def test_a_peer_first_seen_without_a_port_keeps_the_documented_default(tmp_path):
    """A brand-new row still starts at 8888 — that is the protocol default."""
    node_id = "dpc-node-" + "c" * 32
    cache = PeerCache(tmp_path / "peer_cache.json")
    cache.add_or_update_peer(node_id=node_id, direct_ip="10.0.0.7",
                             supports_direct=True)

    assert cache.get_peer(node_id).last_direct_port == 8888
