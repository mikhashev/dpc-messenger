"""A direct link that goes silent is ended, and one that answers is kept.

Twice, 2026-10-27 and 10-28, the Linux node held a direct TLS link to the
Windows node for 50 minutes to two hours after the far end was gone: the
kernel clocks (keepalive, retransmission) never fired, inferred to be a VPN on
the path answering TCP for a TLS end that no longer existed. Only a frame the
peer answers across TLS proves it is there (DPTP 1.13): a node that advertised
`link_ping` in HELLO or HELLO_ACK is sent LINK_PING after a third of
`link_silence_timeout` with no frame from it, any frame resets the clock, and
at the threshold the link is aborted into the listener's one cleanup. A peer
that did not advertise it is never pinged and never cut.

Cross-platform: loopback TLS through asyncio.start_server on both sides; no
socket options, no signals; every file in tmp_path, never ~/.dpc. The clocks
are a second or less, so the bounds carry a scheduling margin, not a race.
"""

import asyncio
import base64
import datetime
import logging
import re
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
from dpc_client_core.message_router import MessageRouter
from dpc_client_core.own_addresses import OwnAddresses
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import PeerCache

LOOPBACK = "127.0.0.1"
THRESHOLD = 0.9  # link_silence_timeout for every manager here
INTERVAL = THRESHOLD / 3  # the LINK_PING interval it derives
LOGGER = "dpc_client_core.p2p_manager"


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
        hello_hash = hello_content_hash(
            nonce=nonce, listener_node_id=listener_node_id, dialer_node_id=self.node_id
        )
        return CommitSigner(self.node_id, self.key).sign_commit(hello_hash)


async def _nothing(*args, **kwargs):
    return None


def _manager(tmp_path: Path, stem: str) -> P2PManager:
    """A P2PManager whose every file lives in tmp_path — `__init__` reads ~/.dpc."""
    own = _Identity()
    cert_file, key_file = own.write_files(tmp_path, stem)
    manager = P2PManager.__new__(P2PManager)
    manager.node_id = own.node_id
    manager.cert_file = cert_file
    manager.key_file = key_file
    manager.display_name = stem
    manager.settings = None
    manager.own_addresses = OwnAddresses()
    manager.peers = {}
    manager._peer_listener_tasks = {}
    manager._peer_watchdog_tasks = {}
    manager._link_silence_timeout = THRESHOLD
    manager._intentional_disconnects = set()
    manager._hub_client_refs = {}
    manager._pending_webrtc = {}
    manager._ice_candidates_buffer = {}
    manager.dht_manager = None
    manager.announce_to_dht = _nothing
    manager.on_peer_list_change = None
    manager.on_message_received = None
    manager.on_peer_disconnected = None
    manager.peer_cache = PeerCache(tmp_path / f"{stem}_peer_cache.json")
    manager._persist_peer_certificate = lambda nid, pem: True
    manager._failed_hello_counts = {}
    manager._rate_limit_max_failures = 10
    manager._rate_limit_window_seconds = 300
    manager._pending_hello_counts = {}
    manager._hello_timeout = 10.0
    manager._max_pending_hellos_per_ip = 8
    return manager


def _record(manager: P2PManager):
    """What reached the router, and every loss the manager reported."""
    routed: list = []
    lost: list = []

    async def on_message(node_id, message):
        routed.append(message.get("command"))

    async def on_lost(node_id):
        lost.append((node_id, time.monotonic()))

    manager.on_message_received = on_message
    manager.on_peer_disconnected = on_lost
    return routed, lost


class _Remote:
    """A node that answers the HELLO handshake, then runs `after_ack(reader, writer)`.

    `link_ping` puts the capability into its HELLO_ACK; without `after_ack` it
    reads and records every frame and answers none — a peer that stopped talking.
    """

    def __init__(self, tmp_path: Path, *, link_ping: bool, after_ack=None):
        self.identity = _Identity()
        self.cert_file, self.key_file = self.identity.write_files(tmp_path, "remote")
        self.link_ping = link_ping
        self.after_ack = after_ack
        self.hello = None
        self.received: list = []
        self.closed = asyncio.Event()
        self.writers: list = []
        self.server = None
        self.port = 0

    async def start(self):
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(certfile=str(self.cert_file), keyfile=str(self.key_file))
        self.server = await asyncio.start_server(self._handle, LOOPBACK, 0, ssl=ctx)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        try:
            await write_message(writer, {
                "command": "HELLO_CHALLENGE",
                "payload": {
                    "nonce": base64.b64encode(b"n" * 32).decode(),
                    "hello_preimage_version": HELLO_PREIMAGE_VERSION,
                },
            })
            self.hello = await read_message(reader)
            ack = {"command": "HELLO_ACK", "status": "OK",
                   "name": "remote", "node_id": self.identity.node_id}
            if self.link_ping:
                ack["link_ping"] = 1
            await write_message(writer, ack)
            if self.after_ack is not None:
                await self.after_ack(reader, writer)
            while True:
                message = await _next(reader)
                if message is None:
                    break
                self.received.append(message.get("command"))
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            self.closed.set()
            writer.close()

    async def stop(self):
        for writer in self.writers:
            writer.close()
        if self.writers:
            await asyncio.wait_for(self.closed.wait(), timeout=5)
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


async def _finish(*managers: P2PManager, remotes=(), servers=()):
    for manager in managers:
        for peer in list(manager.peers.values()):
            await peer.close()
    for manager in managers:
        tasks = list(manager._peer_listener_tasks.values()) + list(manager._peer_watchdog_tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    for remote in remotes:
        await remote.stop()
    for server in servers:
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


def _warnings(caplog) -> list:
    return [r.getMessage() for r in caplog.records
            if r.name == LOGGER and r.levelno == logging.WARNING]


# --- a silent peer that advertised link_ping ---------------------------------------------


@pytest.mark.asyncio
async def test_a_silent_peer_is_declared_dead_once_within_the_threshold_and_one_interval(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    routed, lost = _record(manager)
    remote = _Remote(tmp_path, link_ping=True)
    await remote.start()
    target = remote.identity.node_id
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
            connected_at = time.monotonic()
            # The dialler advertised the capability too.
            assert remote.hello["payload"]["link_ping"] == 1
            await _until(lambda: target in manager._peer_watchdog_tasks)

            await _until(lambda: lost, timeout=THRESHOLD + INTERVAL + 2)
            elapsed = lost[0][1] - connected_at
            # A third of the way in it asked, and nobody answered.
            assert "LINK_PING" in remote.received
            await asyncio.wait_for(remote.closed.wait(), timeout=5)
            await asyncio.sleep(2 * THRESHOLD)

        assert THRESHOLD - 0.1 <= elapsed <= THRESHOLD + INTERVAL, elapsed
        # Once: one loss reported, one WARNING, and it names the threshold.
        assert [node for node, _ in lost] == [target]
        warnings = _warnings(caplog)
        assert len(warnings) == 1, warnings
        assert target in warnings[0] and f"(threshold {THRESHOLD:g}s)" in warnings[0]
        # The listener's one cleanup ran: nothing of the link is left.
        assert target not in manager.peers
        assert target not in manager._peer_listener_tasks
        assert target not in manager._peer_watchdog_tasks
        # Neither the pings nor anything about them reached the router.
        assert "LINK_PING" not in routed and "LINK_PONG" not in routed
    finally:
        await _finish(manager, remotes=[remote])


# --- what the dead verdict says about the path -------------------------------------------


@pytest.mark.asyncio
async def test_the_dead_verdict_names_both_ends_who_dialled_and_when_the_last_frame_came(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    manager.own_addresses.learn(LOOPBACK, "test")
    _, lost = _record(manager)
    remote = _Remote(tmp_path, link_ping=True)
    await remote.start()
    target = remote.identity.node_id
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            peer = await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
            local_port = peer.writer.get_extra_info("sockname")[1]
            await _until(lambda: lost, timeout=THRESHOLD + INTERVAL + 2)

        lines = [r.getMessage() for r in caplog.records
                 if r.name == LOGGER and r.getMessage().startswith("Silent link to ")]
        assert len(lines) == 1, lines
        line = lines[0]
        assert target in line
        assert f"peer {LOOPBACK}:{remote.port}" in line
        assert f"local {LOOPBACK}:{local_port}" in line
        assert "dialled by this node" in line
        assert re.search(r"last frame at \d\d:\d\d:\d\d", line), line
        assert "peer address is this node's own: yes" in line
        assert "local address still on an interface: yes" in line
    finally:
        await _finish(manager, remotes=[remote])


@pytest.mark.asyncio
async def test_an_inbound_silent_peer_is_ended_too_and_the_line_says_the_peer_dialled(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    _, lost = _record(manager)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(manager.cert_file), keyfile=str(manager.key_file))
    server = await asyncio.start_server(manager._handle_direct_connection, LOOPBACK, 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    who = _Identity()
    writer = None
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            client = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            client.check_hostname = False
            client.verify_mode = ssl.CERT_NONE
            reader, writer = await asyncio.open_connection(LOOPBACK, port, ssl=client)
            challenge = await read_message(reader)
            nonce = base64.b64decode(challenge["payload"]["nonce"])
            await write_message(writer, {
                "command": "HELLO",
                "payload": {
                    "node_id": who.node_id, "name": "tester", "cert_pem": who.cert_pem,
                    "hello_preimage_version": HELLO_PREIMAGE_VERSION,
                    "hello_signature": who.sign_hello(nonce, listener_node_id=manager.node_id),
                    "link_ping": 1,
                },
            })
            ack = await asyncio.wait_for(read_message(reader), timeout=5)
            assert ack["command"] == "HELLO_ACK" and ack["link_ping"] == 1, ack
            # Read and never answer, as a peer whose path died would.
            seen: list = []

            async def drain():
                while (frame := await _next(reader)) is not None:
                    seen.append(frame.get("command"))

            reading = asyncio.create_task(drain())
            await _until(lambda: lost, timeout=THRESHOLD + INTERVAL + 2)
            await asyncio.wait_for(reading, timeout=5)

        assert "LINK_PING" in seen
        assert [node for node, _ in lost] == [who.node_id]
        lines = [r.getMessage() for r in caplog.records
                 if r.name == LOGGER and r.getMessage().startswith("Silent link to ")]
        assert len(lines) == 1 and "dialled by the peer" in lines[0], lines
    finally:
        if writer is not None:
            writer.close()
        await _finish(manager, servers=[server])


# --- a peer that answers, or talks anyway ------------------------------------------------


@pytest.mark.asyncio
async def test_two_nodes_that_answer_pings_stay_connected_over_several_thresholds(tmp_path, caplog):
    listener = _manager(tmp_path, "listener")
    dialler = _manager(tmp_path, "dialler")
    at_listener, lost_listener = _record(listener)
    at_dialler, lost_dialler = _record(dialler)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(listener.cert_file), keyfile=str(listener.key_file))
    server = await asyncio.start_server(listener._handle_direct_connection, LOOPBACK, 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await dialler.connect_directly(LOOPBACK, port, listener.node_id, timeout=10)
            await _until(lambda: dialler.node_id in listener.peers)
            await asyncio.sleep(4 * THRESHOLD)

        assert listener.node_id in dialler.peers and dialler.node_id in listener.peers
        assert lost_listener == [] and lost_dialler == []
        assert _warnings(caplog) == []
        # The links were idle, so it was the pongs that kept them.
        pongs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("LINK_PONG from")]
        assert any(listener.node_id in m for m in pongs) and any(dialler.node_id in m for m in pongs)
        # Answered below the router: neither command reached it on either side.
        for routed in (at_listener, at_dialler):
            assert "LINK_PING" not in routed and "LINK_PONG" not in routed, routed
    finally:
        await _finish(dialler, listener, servers=[server])


@pytest.mark.asyncio
async def test_a_peer_that_keeps_sending_other_frames_is_never_pinged(tmp_path, caplog):
    stop = asyncio.Event()
    talking: list = []

    async def chatter(reader, writer):
        async def talk():
            while not stop.is_set():
                await write_message(writer, {"command": "SEND_TEXT", "payload": {"text": "."}})
                await asyncio.sleep(INTERVAL / 2)
        talking.append(asyncio.create_task(talk()))

    manager = _manager(tmp_path, "local")
    routed, lost = _record(manager)
    remote = _Remote(tmp_path, link_ping=True, after_ack=chatter)
    await remote.start()
    target = remote.identity.node_id
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
            await asyncio.sleep(4 * THRESHOLD)
            stop.set()

        assert target in manager.peers and lost == []
        assert "LINK_PING" not in remote.received, remote.received
        assert routed.count("SEND_TEXT") >= 10
        assert _warnings(caplog) == []
    finally:
        stop.set()
        for task in talking:
            task.cancel()
        await asyncio.gather(*talking, return_exceptions=True)
        await _finish(manager, remotes=[remote])


@pytest.mark.asyncio
async def test_a_large_frame_dripped_slower_than_the_threshold_is_not_cut(tmp_path, caplog):
    piece = 64 * 1024
    pause = INTERVAL / 4
    body = b'{"command": "BIG", "payload": {"pad": "' + b"x" * (1024 * 1024) + b'"}}'
    frame = f"{len(body):010d}".encode() + body
    pieces = [frame[i:i + piece] for i in range(0, len(frame), piece)]
    # Slower than the threshold in all, with bytes every quarter interval.
    assert len(pieces) * pause > THRESHOLD * 1.3

    async def drip(reader, writer):
        for chunk in pieces:
            writer.write(chunk)
            await writer.drain()
            await asyncio.sleep(pause)

    manager = _manager(tmp_path, "local")
    routed, lost = _record(manager)
    remote = _Remote(tmp_path, link_ping=True, after_ack=drip)
    await remote.start()
    target = remote.identity.node_id
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            started = time.monotonic()
            await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
            await _until(lambda: "BIG" in routed or lost, timeout=10)
            took = time.monotonic() - started

        assert took > THRESHOLD, took
        assert "BIG" in routed and lost == [], (routed, lost)
        assert target in manager.peers
        assert _warnings(caplog) == []
    finally:
        await _finish(manager, remotes=[remote])


# --- a peer without the capability -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_without_link_ping_is_never_pinged_and_never_cut(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    _, lost = _record(manager)
    remote = _Remote(tmp_path, link_ping=False)
    await remote.start()
    target = remote.identity.node_id
    try:
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await manager.connect_directly(LOOPBACK, remote.port, target, timeout=10)
            await _until(lambda: target in manager._peer_listener_tasks)
            await asyncio.sleep(3 * THRESHOLD)
            assert target not in manager._peer_watchdog_tasks

        assert target in manager.peers and lost == []
        assert "LINK_PING" not in remote.received, remote.received
        assert not remote.closed.is_set()
        assert _warnings(caplog) == []
    finally:
        await _finish(manager, remotes=[remote])


@pytest.mark.asyncio
async def test_an_unknown_link_ping_at_an_old_node_is_a_router_warning_and_the_link_stays(tmp_path, caplog):
    """The old node is the remote: no capability, every frame handed to a real router."""
    router = MessageRouter()

    async def old_node(reader, writer):
        while (message := await _next(reader)) is not None:
            old.received.append(message.get("command"))
            await router.route_message("dpc-node-" + "cd" * 16, message)

    manager = _manager(tmp_path, "local")
    _, lost = _record(manager)
    old = _Remote(tmp_path, link_ping=False, after_ack=old_node)
    await old.start()
    target = old.identity.node_id
    try:
        with caplog.at_level(logging.WARNING, logger="dpc_client_core.message_router"):
            peer = await manager.connect_directly(LOOPBACK, old.port, target, timeout=10)
            await peer.send({"command": "LINK_PING", "payload": {"sent_at": 1.0}})
            await _until(lambda: "LINK_PING" in old.received)
            await peer.send({"command": "SEND_TEXT", "payload": {"text": "still here"}})
            await _until(lambda: "SEND_TEXT" in old.received)
            await asyncio.sleep(THRESHOLD)

        assert any(r.getMessage() == "Unknown P2P message command: LINK_PING" for r in caplog.records)
        assert not old.closed.is_set()
        assert target in manager.peers and lost == []
    finally:
        await _finish(manager, remotes=[old])


# --- which source made the peer's address ours --------------------------------------------


class _Writer:
    def __init__(self, peer_ip, local_ip):
        self._info = {"peername": (peer_ip, 8888), "sockname": (local_ip, 50000)}

    def get_extra_info(self, name):
        return self._info.get(name)


def _dead_link_line(manager, peer_ip, caplog):
    class _Peer:
        node_id = "dpc-node-" + "0" * 32
        writer = _Writer(peer_ip, "192.0.2.10")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        manager._log_dead_link_path(_Peer(), 95.0, "out")
    lines = [r.getMessage() for r in caplog.records
             if r.name == LOGGER and r.getMessage().startswith("Silent link to ")]
    assert len(lines) == 1, lines
    return lines[0]


def test_an_address_found_on_an_interface_is_named_as_that_source(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    manager.own_addresses.learn("192.0.2.77", "lan interface")
    manager.own_addresses.learn("192.0.2.77", "default route")
    line = _dead_link_line(manager, "192.0.2.77", caplog)
    assert "peer address is this node's own: yes (lan interface, default route);" in line


def test_a_stun_address_that_is_the_current_external_one_says_current(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    manager._external_ip = None
    asyncio.run(manager.update_dht_ip("198.51.100.5"))
    line = _dead_link_line(manager, "198.51.100.5", caplog)
    assert "own: yes (stun: the current external address);" in line


def test_a_stun_address_the_exit_has_since_left_says_earlier(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    asyncio.run(manager.update_dht_ip("198.51.100.5"))
    asyncio.run(manager.update_dht_ip("203.0.113.9"))
    line = _dead_link_line(manager, "198.51.100.5", caplog)
    assert "own: yes (stun: an earlier external address);" in line
    caplog.clear()
    line = _dead_link_line(manager, "203.0.113.9", caplog)
    assert "own: yes (stun: the current external address);" in line


def test_an_address_that_is_not_ours_carries_no_parentheses(tmp_path, caplog):
    manager = _manager(tmp_path, "local")
    manager.own_addresses.learn("192.0.2.77", "lan interface")
    line = _dead_link_line(manager, "203.0.113.50", caplog)
    assert "peer address is this node's own: no;" in line
