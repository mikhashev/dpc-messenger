"""A cached address that now reaches this very node is a stale route, not an attack.

Behind a shared exit, or after a peer moved away, the one address cached for a
peer can end at this node's own listener. The dial then meets a certificate
whose CN is this node's id, and `connect_directly` used to log it as a failed
certificate check ("MITM attack detected", with a traceback) and kept the
address, so every start repeated it. A certificate that is this node's own is a
self-dial: one INFO line, the address leaves the peer's cache entry, and the
dial fails with a short reason. A certificate of some third node keeps the old
wording.

Everything runs against a localhost TLS listener and a PeerCache in tmp_path.
"""

import asyncio
import datetime
import logging
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_client_core.p2p_manager import P2PManager, SelfDialError
from dpc_client_core.peer_cache import PeerCache, dial_in_order

# RFC 5737 documentation addresses: other addresses of the same peer.
OTHER_IP = "192.0.2.10"
THIRD_IP = "198.51.100.20"


def _identity():
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
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return node_id, cert_pem, key_pem


async def _tls_server(cert_pem, key_pem, tmp_path, name):
    cert_file = tmp_path / f"{name}.crt"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_text(cert_pem, encoding="utf-8")
    key_file.write_text(key_pem, encoding="utf-8")
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    async def _handle(reader, writer):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


def _manager(own_id, tmp_path, peer_id, port):
    """A dialler that knows `peer_id` at the listener's address and at two others."""
    manager = P2PManager.__new__(P2PManager)
    manager.node_id = own_id
    manager.peer_cache = PeerCache(tmp_path / "known_peers.json")
    manager._persist_peer_certificate = lambda nid, pem: True
    manager.peer_cache.add_or_update_peer(
        node_id=peer_id, direct_ip=OTHER_IP, direct_port=8888,
        supports_direct=True, direction="out",
    )
    manager.peer_cache.add_or_update_peer(
        node_id=peer_id, direct_ip=THIRD_IP, direct_port=8889,
        supports_direct=True, direction="out",
    )
    # The address under test is the newest, so it is also the primary one.
    manager.peer_cache.add_or_update_peer(
        node_id=peer_id, direct_ip="127.0.0.1", direct_port=port,
        supports_direct=True, direction="out",
    )
    return manager


def _addresses(manager, peer_id):
    return {(e["ip"], e["port"]) for e in manager.peer_cache.get_peer(peer_id).addresses}


@pytest.mark.asyncio
async def test_a_dial_that_meets_this_nodes_own_certificate_drops_the_address_quietly(tmp_path, caplog):
    own_id, own_cert, own_key = _identity()
    peer_id, _, _ = _identity()

    server, port = await _tls_server(own_cert, own_key, tmp_path, "own")
    try:
        manager = _manager(own_id, tmp_path, peer_id, port)
        assert ("127.0.0.1", port) in _addresses(manager, peer_id)
        with caplog.at_level(logging.DEBUG, logger="dpc_client_core"):
            with pytest.raises(SelfDialError) as raised:
                await manager.connect_directly("127.0.0.1", port, peer_id, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert "leads back to this node" in str(raised.value)
    ours = [r for r in caplog.records if r.name.startswith("dpc_client_core")]
    text = " ".join(r.getMessage() for r in ours)
    assert not [r for r in ours if r.levelno >= logging.ERROR]
    assert not any(r.exc_info for r in ours)
    assert "MITM" not in text and "Certificate validation failed" not in text
    assert [r for r in ours if r.levelno == logging.INFO and "leads back to this node" in r.getMessage()]

    # Gone from the entry; its other addresses stay, and the primary follows.
    assert _addresses(manager, peer_id) == {(OTHER_IP, 8888), (THIRD_IP, 8889)}
    peer = manager.peer_cache.get_peer(peer_id)
    assert (peer.last_direct_ip, peer.last_direct_port) in {(OTHER_IP, 8888), (THIRD_IP, 8889)}
    # And the file, not only the object.
    reloaded = PeerCache(tmp_path / "known_peers.json").get_peer(peer_id)
    assert {(e["ip"], e["port"]) for e in reloaded.addresses} == {(OTHER_IP, 8888), (THIRD_IP, 8889)}


@pytest.mark.asyncio
async def test_the_reason_the_orchestrator_prints_is_short_and_calm(tmp_path):
    own_id, own_cert, own_key = _identity()
    peer_id, _, _ = _identity()

    server, port = await _tls_server(own_cert, own_key, tmp_path, "own")
    try:
        manager = _manager(own_id, tmp_path, peer_id, port)

        async def _dial(ip, p, budget):
            return await manager.connect_directly(ip, p, peer_id, timeout=budget)

        connection, entry, error = await dial_in_order(
            _dial, [{"ip": "127.0.0.1", "port": port}], total_timeout=5.0
        )
    finally:
        server.close()
        await server.wait_closed()

    assert connection is None and entry is None
    assert str(error) == f"127.0.0.1:{port} leads back to this node"


@pytest.mark.asyncio
async def test_a_third_nodes_certificate_keeps_the_old_error_and_the_address(tmp_path, caplog):
    own_id, _, _ = _identity()
    peer_id, _, _ = _identity()
    _, third_cert, third_key = _identity()

    server, port = await _tls_server(third_cert, third_key, tmp_path, "third")
    try:
        manager = _manager(own_id, tmp_path, peer_id, port)
        before = _addresses(manager, peer_id)
        with caplog.at_level(logging.DEBUG, logger="dpc_client_core"):
            with pytest.raises(ConnectionError) as raised:
                await manager.connect_directly("127.0.0.1", port, peer_id, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert not isinstance(raised.value, SelfDialError)
    assert "MITM attack detected" in str(raised.value)
    errors = [r.getMessage() for r in caplog.records
              if r.name.startswith("dpc_client_core") and r.levelno >= logging.ERROR]
    assert any("Certificate validation failed: CN=" in m for m in errors)
    assert _addresses(manager, peer_id) == before


def test_removing_an_address_the_entry_never_held_changes_nothing(tmp_path):
    cache = PeerCache(tmp_path / "known_peers.json")
    cache.add_or_update_peer(node_id="dpc-node-aa", direct_ip=OTHER_IP, direct_port=8888, supports_direct=True)

    assert cache.remove_address("dpc-node-aa", THIRD_IP, 8888) is False
    assert cache.remove_address("dpc-node-unknown", OTHER_IP, 8888) is False
    assert cache.remove_address("dpc-node-aa", OTHER_IP, 9999) is False
    assert [(e["ip"], e["port"]) for e in cache.get_peer("dpc-node-aa").addresses] == [(OTHER_IP, 8888)]


def test_removing_the_only_address_leaves_the_entry_without_a_primary_one(tmp_path):
    cache = PeerCache(tmp_path / "known_peers.json")
    cache.add_or_update_peer(node_id="dpc-node-aa", direct_ip=OTHER_IP, direct_port=8888, supports_direct=True)

    assert cache.remove_address("dpc-node-aa", OTHER_IP, 8888) is True
    peer = cache.get_peer("dpc-node-aa")
    assert peer.addresses == [] and peer.last_direct_ip is None


def test_removing_an_address_also_leaves_the_advertised_local_addresses(tmp_path):
    cache = PeerCache(tmp_path / "known_peers.json")
    cache.add_or_update_peer(node_id="dpc-node-aa", direct_ip=OTHER_IP, direct_port=8888, supports_direct=True)
    cache.set_local_addresses("dpc-node-aa", ["192.168.1.5:8888", "10.0.0.7:8888"])

    assert cache.remove_address("dpc-node-aa", "192.168.1.5", 8888) is True
    assert cache.get_peer("dpc-node-aa").metadata["local_addresses"] == ["10.0.0.7:8888"]
