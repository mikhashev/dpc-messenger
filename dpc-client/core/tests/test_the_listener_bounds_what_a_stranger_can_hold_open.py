"""The listener bounds what a stranger can hold open.

Inbound, the server context asks for no client certificate, so any TCP client
that completes TLS reaches the HELLO read — and that read had no `wait_for`,
while `_is_rate_limited` counted only HELLOs that *failed*. A client that
never finishes a HELLO never fails one: N idle connections from one address
were never counted, and each held its handler task for ever (ADR-041 M7, D8).

Three guards now stand on that path — a bounded HELLO read whose expiry
counts as a failed HELLO for the rate limiter, a per-address count of
connections between accept and HELLO_ACK refused silently past a ceiling, and
the frame cap in the protocol library — and the honest handshake has to get
through all three, which is the last test here.

Cross-platform: loopback TLS through asyncio.start_server, the call the real
listener makes in `start_server`; no socket options, no signals.
"""

import asyncio
import datetime
import ssl
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.protocol import read_message
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import PeerCache

LOOPBACK = "127.0.0.1"


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


def _manager(tmp_path: Path, name: str, *, hello_timeout: float = 10.0, max_pending: int = 8):
    """A P2PManager whose every file lives in tmp_path, guards set by hand.

    `__init__` reads ~/.dpc, so the fields are what the listener path touches
    — the same shape the reconnect test uses for the dial side.
    """
    node_id, cert_file, key_file = _identity(tmp_path, name)

    manager = P2PManager.__new__(P2PManager)
    manager.node_id = node_id
    manager.cert_file = cert_file
    manager.key_file = key_file
    manager.display_name = name
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
    manager.peer_cache = PeerCache(tmp_path / f"{name}_peer_cache.json")
    manager._persist_peer_certificate = lambda nid, pem: True
    manager._failed_hello_counts = {}
    manager._rate_limit_max_failures = 10
    manager._rate_limit_window_seconds = 300
    manager._pending_hello_counts = {}
    manager._hello_timeout = hello_timeout
    manager._max_pending_hellos_per_ip = max_pending
    return manager


async def _serve(manager):
    """The real listener callback behind a loopback TLS server on a free port."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(manager.cert_file), keyfile=str(manager.key_file))
    server = await asyncio.start_server(manager._handle_direct_connection, LOOPBACK, 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1]


async def _open(port: int):
    """A client that completes TLS and nothing more — the stranger's shape."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncio.open_connection(LOOPBACK, port, ssl=ctx)


async def _next(reader):
    """The next frame, or None once the far end has hung up either way."""
    try:
        return await read_message(reader)
    except ConnectionError:
        return None


async def _until(condition, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not true within {timeout}s"
        await asyncio.sleep(0.02)


async def _stop(server):
    server.close()
    await server.wait_closed()


# --- the bounded read ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_peer_that_completes_tls_and_never_says_hello_is_cut_off_and_counted(tmp_path):
    """The read that had no clock, and the failure the rate limiter never saw."""
    manager = _manager(tmp_path, "listener", hello_timeout=0.3)
    server, port = await _serve(manager)
    try:
        reader, writer = await _open(port)
        try:
            assert (await read_message(reader))["command"] == "HELLO_CHALLENGE"
            started = time.monotonic()

            # Without the clock this waits the full five seconds and fails there.
            hung_up = await asyncio.wait_for(_next(reader), timeout=5)

            assert hung_up is None
            assert time.monotonic() - started < 3, "the listener outlived its hello_timeout"
            await _until(lambda: len(manager._failed_hello_counts.get(LOOPBACK, [])) == 1)
            await _until(lambda: LOOPBACK not in manager._pending_hello_counts)
        finally:
            writer.close()
    finally:
        await _stop(server)


# --- the per-address count -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_ninth_idle_connection_from_one_address_is_refused_while_eight_wait(tmp_path):
    """Eight strangers are counted; the ninth is closed before any challenge.

    The timeout is long on purpose: the eight must still be waiting when the
    ninth arrives, or the count would be measuring the clock instead.
    """
    manager = _manager(tmp_path, "listener", hello_timeout=30.0, max_pending=8)
    server, port = await _serve(manager)
    held = []
    try:
        for _ in range(8):
            reader, writer = await _open(port)
            held.append(writer)
            assert (await read_message(reader))["command"] == "HELLO_CHALLENGE"
        assert manager._pending_hello_counts[LOOPBACK] == 8

        reader, writer = await _open(port)
        ninth = await asyncio.wait_for(_next(reader), timeout=5)
        writer.close()

        assert ninth is None, f"the ninth was admitted: {ninth}"
        assert manager._pending_hello_counts[LOOPBACK] == 8
        # Silent like the rate limit: a refusal is not a failed HELLO.
        assert LOOPBACK not in manager._failed_hello_counts
    finally:
        for writer in held:
            writer.close()
        try:
            await _until(lambda: LOOPBACK not in manager._pending_hello_counts, timeout=10)
        finally:
            await _stop(server)

    assert LOOPBACK not in manager._pending_hello_counts


# --- the frame cap, on the listener's own path ---------------------------------


@pytest.mark.asyncio
async def test_a_ten_digit_length_header_after_tls_is_refused_not_buffered(tmp_path):
    """The board entry's falsifier, over loopback: the worst header, sent in
    place of HELLO. The listener hangs up at once, the slot is released, and
    the refusal is one failed HELLO — never a nine-gigabyte read."""
    manager = _manager(tmp_path, "listener", hello_timeout=30.0)
    server, port = await _serve(manager)
    try:
        reader, writer = await _open(port)
        try:
            assert (await read_message(reader))["command"] == "HELLO_CHALLENGE"
            writer.write(b"9999999999" + b"{")
            await writer.drain()
            started = time.monotonic()

            hung_up = await asyncio.wait_for(_next(reader), timeout=5)

            assert hung_up is None
            assert time.monotonic() - started < 3, "the listener waited for the declared bytes"
            await _until(lambda: len(manager._failed_hello_counts.get(LOOPBACK, [])) == 1)
            await _until(lambda: LOOPBACK not in manager._pending_hello_counts)
        finally:
            writer.close()
    finally:
        await _stop(server)


# --- the honest handshake ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_honest_handshake_still_gets_through_all_three_guards(tmp_path):
    """The regression: the real dial against the real listener, both guarded."""
    listener = _manager(tmp_path, "listener", hello_timeout=10.0)
    dialler = _manager(tmp_path, "dialler")
    server, port = await _serve(listener)
    try:
        await dialler.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)

        assert listener.node_id in dialler.peers
        await _until(lambda: dialler.node_id in listener.peers)
        assert listener._pending_hello_counts == {}
        assert listener._failed_hello_counts == {}
    finally:
        for peer in list(dialler.peers.values()):
            await peer.close()
        tasks = list(listener._peer_listener_tasks.values()) + list(dialler._peer_listener_tasks.values())
        await asyncio.gather(*tasks, return_exceptions=True)
        await _stop(server)


# --- the ceilings come from configuration ---------------------------------------


def test_both_ceilings_are_read_from_configuration(tmp_path):
    from dpc_client_core.settings import Settings

    fresh = Settings(tmp_path)
    assert fresh.get_hello_timeout() == 10.0
    assert fresh.get_max_pending_hellos_per_ip() == 8

    (tmp_path / "config.ini").write_text(
        "[connection]\nhello_timeout = 2.5\nmax_pending_hellos_per_ip = 3\n"
    )
    tuned = Settings(tmp_path)
    assert tuned.get_hello_timeout() == 2.5
    assert tuned.get_max_pending_hellos_per_ip() == 3
