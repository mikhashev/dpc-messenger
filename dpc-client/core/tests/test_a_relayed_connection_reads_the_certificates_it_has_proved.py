"""THE-GOSSIP-CERTIFICATE-LOOKUP-ASKS-FOR-ATTRIBUTES-NOBODY-SETS-AND-NEVER-READS-THE-CERTIFICATES-ON-DISK, relay site.

RelayedPeerConnection._get_peer_certificate looked a peer's certificate up in the
DHT only, so a node that had already proved a peer's key (and stored it in the
peers directory) still could not encrypt to it over a relay once the DHT was empty.
It now reads the peers store first and keeps its failure contract: ConnectionError
when nothing has the certificate.
"""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from datetime import datetime, timedelta, timezone

from dpc_client_core.models.relay_node import RelayNode
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.transports.relayed_connection import RelayedPeerConnection
from dpc_protocol.crypto import decrypt_with_private_key_hybrid, generate_node_id

RELAY_NODE_ID = "dpc-node-relay0000000000000000000000000000000"


def _certificate():
    """(cert_pem, private_key, node_id) with the node_id derived from the key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("utf-8"), key, node_id


@pytest.fixture(autouse=True)
def peers_dir(tmp_path, monkeypatch):
    """An empty peers store, reached through p2p_manager.DPC_HOME_DIR like the writer's."""
    import dpc_client_core.p2p_manager as p2p_module

    monkeypatch.setattr(p2p_module, "DPC_HOME_DIR", tmp_path)
    store = tmp_path / "peers"
    store.mkdir()
    return store


def _dht():
    dht = Mock()
    dht.get_known_peers = Mock(return_value=[Mock(node_id="dpc-node-dht1", ip="10.0.0.1", port=8889)])
    dht.rpc_handler = Mock()
    dht.rpc_handler.find_value = AsyncMock(return_value={})
    return dht


def _connection(peer_id, dht_manager):
    conn = RelayedPeerConnection(
        peer_id=peer_id,
        relay_node=RelayNode(node_id=RELAY_NODE_ID, ip="127.0.0.1", port=9999,
                             available=True, max_peers=10),
        relay_connection=MagicMock(send=AsyncMock()),
        session_id="session-1",
        own_node_id="dpc-node-self000000000000000000000000000000",
        dht_manager=dht_manager,
    )
    conn.running = True
    return conn


def _persist(peers_dir, node_id, cert_pem):
    assert P2PManager._persist_peer_certificate(Mock(), node_id, cert_pem, peers_dir=peers_dir)


@pytest.mark.asyncio
async def test_the_certificate_is_read_from_the_peers_store_without_the_dht(peers_dir):
    cert_pem, _key, node_id = _certificate()
    _persist(peers_dir, node_id, cert_pem)
    dht = _dht()
    conn = _connection(node_id, dht)

    cert = await conn._get_peer_certificate(node_id)

    assert cert.public_bytes(serialization.Encoding.PEM).decode("utf-8") == cert_pem
    assert not dht.rpc_handler.find_value.called


@pytest.mark.asyncio
async def test_the_store_is_read_even_when_there_is_no_dht_manager(peers_dir):
    cert_pem, _key, node_id = _certificate()
    _persist(peers_dir, node_id, cert_pem)

    cert = await _connection(node_id, None)._get_peer_certificate(node_id)

    assert cert is not None


@pytest.mark.asyncio
async def test_a_stored_certificate_of_another_node_is_refused_and_the_failure_stays_a_raise(peers_dir):
    cert_pem, _key, _other_id = _certificate()
    victim = "dpc-node-" + "ab" * 16
    (peers_dir / f"{victim}.crt").write_text(cert_pem, encoding="utf-8")
    dht = _dht()

    with pytest.raises(ConnectionError, match="not found in DHT"):
        await _connection(victim, dht)._get_peer_certificate(victim)
    assert dht.rpc_handler.find_value.called  # it fell through to the DHT


@pytest.mark.asyncio
async def test_nothing_anywhere_still_raises_connection_error(peers_dir):
    node_id = "dpc-node-" + "cd" * 16

    with pytest.raises(ConnectionError):
        await _connection(node_id, _dht())._get_peer_certificate(node_id)
    with pytest.raises(ConnectionError, match="No DHT manager"):
        await _connection(node_id, None)._get_peer_certificate(node_id)


@pytest.mark.asyncio
async def test_send_message_encrypts_to_a_certificate_that_is_only_on_disk(peers_dir):
    """Falsifier: DHT empty, certificate in the peers store, the relay blob decrypts for the peer."""
    cert_pem, key, node_id = _certificate()
    _persist(peers_dir, node_id, cert_pem)
    dht = _dht()
    conn = _connection(node_id, dht)

    await conn.send_message({"command": "SEND_TEXT", "payload": {"text": "hi"}})

    envelope = conn.relay_connection.send.await_args.args[0]
    assert envelope["command"] == "RELAY_MESSAGE"
    plain = decrypt_with_private_key_hybrid(base64.b64decode(envelope["payload"]["data"]), key)
    assert json.loads(plain.decode("utf-8")) == {"command": "SEND_TEXT", "payload": {"text": "hi"}}
    assert not dht.rpc_handler.find_value.called
