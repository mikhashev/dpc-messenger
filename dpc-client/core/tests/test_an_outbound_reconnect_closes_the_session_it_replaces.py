"""An outbound reconnect closes the session it replaces.

`connect_directly` assigned `self.peers[target]` and the listener slot over a
live session without closing it, so the old socket stayed open until the other
end hung up, with no entry pointing at it. The inbound handler was fixed the
same way earlier (see test_a_failed_hello_leaves_the_peer_it_names_connected):
the verified new connection replaces the old one, and the old one is closed
once the new one is registered and listening.

The second question is what a replacement owes `on_peer_disconnected`. In
service.py that callback is the *loss* path: it cancels the peer's file
transfers, forgets its history requests, and starts (or restarts) the
auto-reconnect loop for the peer. None of that fits a peer that is still
connected, and a transfer is addressed by node id through `self.peers`, not
bound to a connection object, so it is not stranded by the swap. A replacement
therefore does not call the callback, on either path; a genuine loss of the
current session still does, once.

Cross-platform: loopback TLS through asyncio.start_server on both sides; no
socket options, no signals; every file in tmp_path, never ~/.dpc.
"""

import asyncio
import base64
import datetime
import logging
import ssl
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_protocol.message_signing import HELLO_PREIMAGE_VERSION, hello_content_hash
from dpc_protocol.protocol import read_message, write_message
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import PeerCache

LOOPBACK = "127.0.0.1"


class _Identity:
    """A node identity held in memory, with its files written on demand."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.node_id = generate_node_id(self.key.public_key())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.node_id)])
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

    def write_files(self, directory: Path, stem: str) -> tuple[Path, Path]:
        cert_file = directory / f"{stem}.crt"
        key_file = directory / f"{stem}.key"
        cert_file.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))
        key_file.write_bytes(
            self.key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
        return cert_file, key_file

    def sign_hello(self, nonce: bytes, *, listener_node_id: str) -> str:
        """The HELLO signature as connect_directly makes it."""
        hello_hash = hello_content_hash(
            nonce=nonce, listener_node_id=listener_node_id, dialer_node_id=self.node_id
        )
        return CommitSigner(self.node_id, self.key).sign_commit(hello_hash)


def _manager(tmp_path: Path) -> tuple[P2PManager, _Identity]:
    """A P2PManager whose every file lives in tmp_path — `__init__` reads ~/.dpc."""
    own = _Identity()
    cert_file, key_file = own.write_files(tmp_path, "local")
    manager = P2PManager.__new__(P2PManager)
    manager.node_id = own.node_id
    manager.cert_file = cert_file
    manager.key_file = key_file
    manager.display_name = "local"
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
    manager.peer_cache = PeerCache(tmp_path / "local_peer_cache.json")
    manager._persist_peer_certificate = lambda nid, pem: True
    manager._failed_hello_counts = {}
    manager._rate_limit_max_failures = 10
    manager._rate_limit_window_seconds = 300
    manager._pending_hello_counts = {}
    manager._hello_timeout = 10.0
    manager._max_pending_hellos_per_ip = 8
    return manager, own


def _record_disconnects(manager: P2PManager) -> list:
    calls: list = []

    async def callback(peer_id):
        calls.append(peer_id)

    manager.on_peer_disconnected = callback
    return calls


class _Session:
    """What the fake remote saw on one accepted connection."""

    def __init__(self):
        self.received: list[str] = []
        self.closed = asyncio.Event()


class _Remote:
    """A node that answers the HELLO handshake as `identity`, one record per connection."""

    def __init__(self, identity: _Identity, tmp_path: Path, stem: str):
        self.identity = identity
        self.cert_file, self.key_file = identity.write_files(tmp_path, stem)
        self.sessions: list[_Session] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.server = None
        self.port = 0

    async def start(self):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=str(self.cert_file), keyfile=str(self.key_file))
        self.server = await asyncio.start_server(self._handle, LOOPBACK, 0, ssl=ctx)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        session = _Session()
        self.sessions.append(session)
        self.writers.append(writer)
        try:
            await write_message(writer, {
                "command": "HELLO_CHALLENGE",
                "payload": {
                    "nonce": base64.b64encode(b"n" * 32).decode(),
                    "hello_preimage_version": HELLO_PREIMAGE_VERSION,
                },
            })
            await read_message(reader)  # the dialler's HELLO
            await write_message(writer, {
                "command": "HELLO_ACK", "status": "OK",
                "name": "remote", "node_id": self.identity.node_id,
            })
            while True:
                message = await _next(reader)
                if message is None:
                    break
                session.received.append(message["command"])
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            session.closed.set()
            writer.close()

    async def stop(self):
        for writer in self.writers:
            writer.close()
        self.server.close()
        await asyncio.wait_for(self.server.wait_closed(), timeout=5)


async def _next(reader):
    """The next frame, or None once the far end has hung up either way."""
    try:
        return await read_message(reader)
    except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
        return None


async def _until(condition, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not true within {timeout}s"
        await asyncio.sleep(0.02)


async def _finish(manager: P2PManager, *remotes: _Remote):
    for peer in list(manager.peers.values()):
        await peer.close()
    tasks = list(manager._peer_listener_tasks.values())
    if tasks:
        await asyncio.wait(tasks, timeout=5)
    for remote in remotes:
        await remote.stop()


# --- the outbound dial replaces and closes -----------------------------------------------


@pytest.mark.asyncio
async def test_an_outbound_dial_to_a_node_we_hold_closes_the_session_it_replaces(tmp_path, caplog):
    manager, _ = _manager(tmp_path)
    remote = _Remote(_Identity(), tmp_path, "remote")
    await remote.start()
    target = remote.identity.node_id
    try:
        await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
        old = manager.peers[target]
        old_listener = manager._peer_listener_tasks[target]

        with caplog.at_level(logging.INFO, logger="dpc_client_core.p2p_manager"):
            new = await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)

        # The new session is the registered one, and it is listening.
        assert new is not old
        assert manager.peers[target] is new
        new_listener = manager._peer_listener_tasks[target]
        assert new_listener is not old_listener and not new_listener.done()

        # The old one is closed at both ends and its listener has ended.
        assert old.writer.is_closing()
        await asyncio.wait_for(remote.sessions[0].closed.wait(), timeout=5)
        await asyncio.wait_for(asyncio.gather(old_listener, return_exceptions=True), timeout=5)
        assert old_listener.done()
        assert any("replaced by a new one" in r.getMessage() and target in r.getMessage()
                   for r in caplog.records)

        # Its ending took nothing of the new one with it: entry, listener, socket.
        await asyncio.sleep(0.1)
        assert manager.peers.get(target) is new
        assert not new.writer.is_closing()
        assert manager._peer_listener_tasks.get(target) is new_listener
        assert not new_listener.done()
        assert not remote.sessions[1].closed.is_set()
        assert target not in manager._intentional_disconnects

        # Nothing leaked: one entry, one listener slot, two sockets of which one is open.
        assert list(manager.peers) == [target]
        assert list(manager._peer_listener_tasks) == [target]
    finally:
        await _finish(manager, remote)


@pytest.mark.asyncio
async def test_a_dial_that_fails_leaves_the_session_we_hold_open(tmp_path):
    manager, _ = _manager(tmp_path)
    remote = _Remote(_Identity(), tmp_path, "remote")
    impostor = _Remote(_Identity(), tmp_path, "impostor")
    await remote.start()
    await impostor.start()
    target = remote.identity.node_id
    try:
        await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
        held = manager.peers[target]
        listener = manager._peer_listener_tasks[target]

        # The target id is answered by someone else's certificate: refused.
        with pytest.raises(ConnectionError):
            await manager.connect_directly(LOOPBACK, impostor.port, target, timeout=10)

        assert manager.peers.get(target) is held
        assert not held.writer.is_closing()
        assert manager._peer_listener_tasks.get(target) is listener
        assert not listener.done()
        assert not remote.sessions[0].closed.is_set()
    finally:
        await _finish(manager, remote, impostor)


# --- what a replacement owes on_peer_disconnected ----------------------------------------


@pytest.mark.asyncio
async def test_a_replacement_runs_no_loss_cleanup_and_the_peer_stays_addressable(tmp_path):
    manager, _ = _manager(tmp_path)
    lost = _record_disconnects(manager)
    remote = _Remote(_Identity(), tmp_path, "remote")
    await remote.start()
    target = remote.identity.node_id
    try:
        await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
        old = manager.peers[target]
        await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
        new = manager.peers[target]
        await asyncio.wait_for(remote.sessions[0].closed.wait(), timeout=5)
        await asyncio.sleep(0.1)

        # The peer is still connected: the loss callback (transfers cancelled,
        # history requests forgotten, reconnect loop started) has not run.
        assert new is not old and manager.peers[target] is new
        assert lost == []

        # A transfer finds the peer by node id, so its next frame rides the new session.
        await manager.send_message_to_peer(target, {"command": "FILE_CHUNK", "payload": {}})
        await _until(lambda: "FILE_CHUNK" in remote.sessions[1].received)
        assert "FILE_CHUNK" not in remote.sessions[0].received

        # A genuine loss of the current session still runs it, once.
        remote.writers[1].close()
        await _until(lambda: lost == [target])
        await _until(lambda: target not in manager.peers)
        await asyncio.sleep(0.1)
        assert lost == [target]
    finally:
        await _finish(manager, remote)


async def _dial_in(port: int, who: _Identity, listener_node_id: str):
    """Dial the manager's listener as `who` and complete an honest HELLO."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(LOOPBACK, port, ssl=ctx)
    challenge = await read_message(reader)
    nonce = base64.b64decode(challenge["payload"]["nonce"])
    await write_message(writer, {
        "command": "HELLO",
        "payload": {
            "node_id": who.node_id, "name": "tester",
            "cert_pem": who.cert_pem, "hello_preimage_version": HELLO_PREIMAGE_VERSION,
            "hello_signature": who.sign_hello(nonce, listener_node_id=listener_node_id),
        },
    })
    answer = await asyncio.wait_for(read_message(reader), timeout=5)
    assert answer["command"] == "HELLO_ACK", answer
    return reader, writer


@pytest.mark.asyncio
async def test_an_inbound_replacement_runs_no_loss_cleanup_either(tmp_path):
    manager, own = _manager(tmp_path)
    lost = _record_disconnects(manager)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(manager.cert_file), keyfile=str(manager.key_file))
    server = await asyncio.start_server(manager._handle_direct_connection, LOOPBACK, 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    who = _Identity()
    first = second = None
    try:
        first = await _dial_in(port, who, manager.node_id)
        await _until(lambda: who.node_id in manager.peers)
        old = manager.peers[who.node_id]
        second = await _dial_in(port, who, manager.node_id)
        await _until(lambda: manager.peers.get(who.node_id) is not old)
        assert await asyncio.wait_for(_next(first[0]), timeout=5) is None
        await asyncio.sleep(0.1)
        assert lost == []
        assert who.node_id in manager.peers
    finally:
        for conn in (first, second):
            if conn:
                conn[1].close()
        for peer in list(manager.peers.values()):
            await peer.close()
        tasks = list(manager._peer_listener_tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)
