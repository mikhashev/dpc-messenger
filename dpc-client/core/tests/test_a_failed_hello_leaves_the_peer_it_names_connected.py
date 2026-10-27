"""A failed HELLO leaves the peer it names connected.

The inbound handler read `node_id` out of the HELLO body before any check, and
its `except` branch closed `self.peers[node_id]` by that name. The frame is
unsigned until the identity check passes, so anyone who completed TLS could name
a connected node, fail the check on purpose, and have the established session of
that node closed — reproduced live three times.

Now only an entry the failing connection registered itself is its to remove
(compared by object, never by name), and a verified reconnect of a node still
held replaces the old session and closes it. The old session's listener, ending
afterwards, must not take the new one down with it.

Cross-platform: loopback TLS through asyncio.start_server, the call the real
listener makes in `start_server`; no socket options, no signals; every file in
tmp_path, never ~/.dpc.
"""

import asyncio
import base64
import datetime
import ssl
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.protocol import read_message, write_message
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import PeerCache

LOOPBACK = "127.0.0.1"


class _Identity:
    """A node identity held in memory: key, node id, certificate."""

    def __init__(self, *, common_name: str | None = None):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.node_id = generate_node_id(self.key.public_key())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name or self.node_id)])
        now = datetime.datetime.now(datetime.timezone.utc)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(self.key, hashes.SHA256())
        )
        self.cert_pem = self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def sign(self, nonce: bytes) -> str:
        sig = self.key.sign(
            nonce,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode()


def _manager(tmp_path: Path) -> P2PManager:
    """A P2PManager whose every file lives in tmp_path — `__init__` reads ~/.dpc."""
    own = _Identity()
    cert_file = tmp_path / "listener.crt"
    key_file = tmp_path / "listener.key"
    cert_file.write_bytes(own.cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        own.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    manager = P2PManager.__new__(P2PManager)
    manager.node_id = own.node_id
    manager.cert_file = cert_file
    manager.key_file = key_file
    manager.display_name = "listener"
    manager.settings = None
    manager.peers = {}
    manager._peer_listener_tasks = {}
    manager._intentional_disconnects = set()
    manager._hub_client_refs = {}
    manager._pending_webrtc = {}
    manager._ice_candidates_buffer = {}
    manager.dht_manager = None
    manager.on_peer_list_change = None
    manager.on_message_received = None
    manager.on_peer_disconnected = None
    manager.peer_cache = PeerCache(tmp_path / "listener_peer_cache.json")
    manager._persist_peer_certificate = lambda nid, pem: True
    manager._failed_hello_counts = {}
    manager._rate_limit_max_failures = 10
    manager._rate_limit_window_seconds = 300
    manager._pending_hello_counts = {}
    manager._hello_timeout = 10.0
    manager._max_pending_hellos_per_ip = 8
    return manager


async def _serve(manager):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(manager.cert_file), keyfile=str(manager.key_file))
    server = await asyncio.start_server(manager._handle_direct_connection, LOOPBACK, 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


async def _hello(port: int, *, claims: str, cert_pem: str, signer: _Identity, sign_nonce: bool = True):
    """Dial, take the challenge, send a HELLO. Returns (reader, writer, answer)."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(LOOPBACK, port, ssl=ctx)
    challenge = await read_message(reader)
    assert challenge["command"] == "HELLO_CHALLENGE"
    nonce = base64.b64decode(challenge["payload"]["nonce"])
    await write_message(writer, {
        "command": "HELLO",
        "payload": {
            "node_id": claims,
            "name": "tester",
            "cert_pem": cert_pem,
            "nonce_signature": signer.sign(nonce if sign_nonce else b"not the nonce"),
        },
    })
    try:
        answer = await asyncio.wait_for(_next(reader), timeout=3)
    except asyncio.TimeoutError:
        answer = "the socket was left open"  # a refusal must hang up, not go quiet
    return reader, writer, answer


async def _honest(port: int, who: _Identity):
    reader, writer, answer = await _hello(port, claims=who.node_id, cert_pem=who.cert_pem, signer=who)
    assert answer and answer["command"] == "HELLO_ACK", f"honest HELLO refused: {answer}"
    return reader, writer


async def _next(reader):
    """The next frame, or None once the far end has hung up either way."""
    try:
        return await read_message(reader)
    except (ConnectionError, asyncio.IncompleteReadError):
        return None


async def _until(condition, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not true within {timeout}s"
        await asyncio.sleep(0.02)


async def _finish(server, manager, *writers):
    for writer in writers:
        writer.close()
    for peer in list(manager.peers.values()):
        await peer.close()
    server.close()
    tasks = list(manager._peer_listener_tasks.values())
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    # A red test must end red, not wait on a connection it left behind.
    await asyncio.wait_for(server.wait_closed(), timeout=5)


# --- a failed HELLO does not touch the session it names ---------------------------


def _forged_cn():
    """Check 1 fails: the certificate's CN is not the id the frame claims."""
    victim, attacker = _Identity(), _Identity()
    return victim, dict(claims=victim.node_id, cert_pem=attacker.cert_pem, signer=attacker)


def _wrong_signature():
    """Check 3 fails: the victim's real certificate, a signature over something else."""
    victim = _Identity()
    return victim, dict(claims=victim.node_id, cert_pem=victim.cert_pem, signer=victim, sign_nonce=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [_forged_cn, _wrong_signature], ids=["foreign_certificate", "bad_signature"])
async def test_a_hello_that_fails_verification_leaves_the_session_it_names_open(tmp_path, failure):
    manager = _manager(tmp_path)
    server, port = await _serve(manager)
    victim, attack = failure()
    held = None
    try:
        held = await _honest(port, victim)
        await _until(lambda: victim.node_id in manager.peers)
        session = manager.peers[victim.node_id]
        listener = manager._peer_listener_tasks[victim.node_id]

        a_reader, a_writer, answer = await _hello(port, **attack)
        try:
            # Refused: the attacker's own socket is hung up, no HELLO_ACK.
            assert answer is None, answer
            await _until(lambda: len(manager._failed_hello_counts.get(LOOPBACK, [])) == 1)
            await _until(lambda: LOOPBACK not in manager._pending_hello_counts)
        finally:
            a_writer.close()

        # The named peer's session is the same object, still registered, still open.
        assert manager.peers.get(victim.node_id) is session
        assert not session.writer.is_closing()
        assert manager._peer_listener_tasks.get(victim.node_id) is listener
        assert not listener.done()
        assert victim.node_id not in manager._intentional_disconnects
        assert not held[1].transport.is_closing()
    finally:
        await _finish(server, manager, *([held[1]] if held else []))


# --- a verified reconnect replaces the old session ---------------------------------


@pytest.mark.asyncio
async def test_a_verified_reconnect_replaces_the_old_session_and_closes_it(tmp_path):
    manager = _manager(tmp_path)
    server, port = await _serve(manager)
    peer_identity = _Identity()
    first = second = None
    try:
        first = await _honest(port, peer_identity)
        await _until(lambda: peer_identity.node_id in manager.peers)
        old = manager.peers[peer_identity.node_id]
        old_listener = manager._peer_listener_tasks[peer_identity.node_id]

        second = await _honest(port, peer_identity)
        await _until(lambda: manager.peers.get(peer_identity.node_id) is not old)
        new = manager.peers[peer_identity.node_id]

        # The old session is closed at both ends and its listener has ended.
        assert old.writer.is_closing()
        assert await asyncio.wait_for(_next(first[0]), timeout=5) is None
        await asyncio.wait_for(asyncio.gather(old_listener, return_exceptions=True), timeout=5)
        assert old_listener.done()

        # And its ending took nothing of the new one with it: entry, listener, socket.
        await asyncio.sleep(0.1)
        assert manager.peers.get(peer_identity.node_id) is new
        assert not new.writer.is_closing()
        new_listener = manager._peer_listener_tasks.get(peer_identity.node_id)
        assert new_listener is not None and new_listener is not old_listener
        assert not new_listener.done()
        assert peer_identity.node_id not in manager._intentional_disconnects
        assert manager._failed_hello_counts == {}
    finally:
        await _finish(server, manager, *[c[1] for c in (first, second) if c])


# --- a failure after registration removes only its own entry -------------------------


@pytest.mark.asyncio
async def test_a_failure_after_registration_removes_only_the_entry_it_registered(tmp_path):
    manager = _manager(tmp_path)
    server, port = await _serve(manager)
    bystander, failing = _Identity(), _Identity()
    held = broken = None
    try:
        held = await _honest(port, bystander)
        await _until(lambda: bystander.node_id in manager.peers)
        bystander_session = manager.peers[bystander.node_id]

        def refuse(**kwargs):
            raise RuntimeError("the peer cache cannot be written")

        manager.peer_cache.add_or_update_peer = refuse

        # Past the identity check and into self.peers, then the handler raises.
        broken = await _honest(port, failing)
        await _until(lambda: len(manager._failed_hello_counts.get(LOOPBACK, [])) == 1)
        await _until(lambda: LOOPBACK not in manager._pending_hello_counts)

        assert failing.node_id not in manager.peers
        assert await asyncio.wait_for(_next(broken[0]), timeout=5) is None
        assert manager.peers.get(bystander.node_id) is bystander_session
        assert not bystander_session.writer.is_closing()
    finally:
        await _finish(server, manager, *[c[1] for c in (held, broken) if c])
