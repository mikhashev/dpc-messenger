"""Until a peer proves its key it gets one clock and a handshake-sized frame.

A direct connection has a phase before the HELLO read and a phase after it.
Before: `asyncio.start_server` was given no `ssl_handshake_timeout`, so a client
that opened TCP and never finished TLS held its socket for asyncio's default of
60 s, six times the `hello_timeout` that only starts once the handler runs.
During: the HELLO read took a frame up to MAX_FRAME_BYTES, 64 MiB, sized for a
proved peer's history — a live probe sent 5 MB and 20 MB first frames after
TLS and the node buffered them until the clock ran out (~11.6 s).

Now the handshake is bounded by the same `hello_timeout`, and every frame read
before HELLO_ACK — the listener's HELLO, the dialler's HELLO_CHALLENGE and
HELLO_ACK — is capped at MAX_HELLO_FRAME_BYTES and refused at its header. After
the proof the 64 MiB cap is back, which the last test holds.

Cross-platform: loopback TLS through the real `P2PManager.start_server`; no
socket options, no signals; every file in tmp_path, never ~/.dpc.
"""

import asyncio
import datetime
import json
import logging
import ssl
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_protocol.protocol import MAX_FRAME_BYTES, MAX_HELLO_FRAME_BYTES, read_message
from dpc_client_core import p2p_manager as p2p_module
from dpc_client_core.own_addresses import OwnAddresses
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.peer_cache import PeerCache

LOOPBACK = "127.0.0.1"


def _identity(tmp_path: Path, name: str):
    """A node identity written to tmp_path, built as generate_identity builds one."""
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
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
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


def _manager(tmp_path: Path, name: str, *, hello_timeout: float = 10.0) -> P2PManager:
    """A P2PManager whose every file lives in tmp_path — `__init__` reads ~/.dpc."""
    node_id, cert_file, key_file = _identity(tmp_path, name)
    manager = P2PManager.__new__(P2PManager)
    manager.node_id = node_id
    manager.cert_file = cert_file
    manager.key_file = key_file
    manager.display_name = name
    manager.settings = SimpleNamespace(get_dht_enabled=lambda: False)
    manager.own_addresses = OwnAddresses()
    manager.peers = {}
    manager._peer_listener_tasks = {}
    manager._intentional_disconnects = set()
    manager._hub_client_refs = {}
    manager._pending_webrtc = {}
    manager._ice_candidates_buffer = {}
    manager._server_task = None
    manager.dht_manager = None
    manager.on_peer_list_change = None
    manager.on_message_received = None
    manager.on_peer_disconnected = None
    manager.peer_cache = PeerCache(tmp_path / f"{name}_peer_cache.json")
    manager._persist_peer_certificate = lambda nid, pem: True
    manager.announce_to_dht = _nothing
    manager._failed_hello_counts = {}
    manager._rate_limit_max_failures = 10
    manager._rate_limit_window_seconds = 300
    manager._pending_hello_counts = {}
    manager._hello_timeout = hello_timeout
    manager._max_pending_hellos_per_ip = 8
    return manager


async def _nothing(*args, **kwargs):
    return None


async def _listen(manager, monkeypatch):
    """The real `start_server` on a free loopback port; returns the port.

    `start_server` keeps only the serve_forever task, so the asyncio server
    it creates is caught on its way out to learn the port it bound.
    """
    created = []
    real_start_server = asyncio.start_server

    async def _recording(*args, **kwargs):
        server = await real_start_server(*args, **kwargs)
        created.append(server)
        return server

    monkeypatch.setattr(p2p_module.asyncio, "start_server", _recording)
    await manager.start_server(host=LOOPBACK, port=0)
    monkeypatch.setattr(p2p_module.asyncio, "start_server", real_start_server)
    assert len(created) == 1
    manager._test_server = created[0]
    return created[0].sockets[0].getsockname()[1]


async def _stop(*managers):
    for manager in managers:
        for peer in list(manager.peers.values()):
            await peer.close()
    for manager in managers:
        tasks = list(manager._peer_listener_tasks.values())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
        server = getattr(manager, "_test_server", None)
        if server is not None:
            server.close()
            await asyncio.wait_for(server.wait_closed(), timeout=5)
        if manager._server_task is not None:
            manager._server_task.cancel()
            await asyncio.gather(manager._server_task, return_exceptions=True)


async def _open_tls(port: int):
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncio.open_connection(LOOPBACK, port, ssl=ctx)


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


# --- phase 1: the TLS handshake --------------------------------------------------


@pytest.mark.asyncio
async def test_a_client_that_opens_tcp_and_never_finishes_tls_is_dropped_within_the_hello_timeout(
    tmp_path, monkeypatch
):
    """Without `ssl_handshake_timeout` asyncio waits 60 s and this waits 5 and fails."""
    manager = _manager(tmp_path, "listener", hello_timeout=0.5)
    port = await _listen(manager, monkeypatch)
    try:
        reader, writer = await asyncio.open_connection(LOOPBACK, port)  # TCP, no TLS
        started = time.monotonic()
        try:
            try:
                left = await asyncio.wait_for(reader.read(), timeout=5)
            except ConnectionError:
                left = b""  # an abort can arrive as a reset; it is the drop all the same
            assert left == b""
            assert time.monotonic() - started < 3, "the handshake outlived hello_timeout"
        finally:
            writer.close()
        # Dropped before the handler ran: neither D8 count saw it (the card's map).
        assert manager._pending_hello_counts == {}
        assert manager._failed_hello_counts == {}
    finally:
        await _stop(manager)


# --- phase 2: the frame before the proof, listener side -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declared", [MAX_HELLO_FRAME_BYTES + 1, 5_000_000, 20_000_000], ids=["cap+1", "5MB", "20MB"]
)
async def test_a_first_frame_declaring_more_than_a_handshake_needs_is_refused_at_its_header(
    tmp_path, monkeypatch, caplog, declared
):
    """The live probe's two sizes and the boundary, sent in place of HELLO.

    Only the header and a few bytes of the body are sent. A listener that
    accepted the declared length would sit waiting for the rest until its
    30-s clock; one that refuses at the header hangs up at once.
    """
    manager = _manager(tmp_path, "listener", hello_timeout=30.0)
    port = await _listen(manager, monkeypatch)
    try:
        reader, writer = await _open_tls(port)
        try:
            assert (await read_message(reader))["command"] == "HELLO_CHALLENGE"
            with caplog.at_level(logging.WARNING):
                writer.write(f"{declared:010d}".encode() + b'{"command": "HELLO", "marker": "PAYLOAD-MARKER')
                await writer.drain()
                started = time.monotonic()

                hung_up = await asyncio.wait_for(_next(reader), timeout=5)

                assert hung_up is None
                assert time.monotonic() - started < 3, "the listener waited for the declared body"
                await _until(lambda: len(manager._failed_hello_counts.get(LOOPBACK, [])) == 1)
                await _until(lambda: LOOPBACK not in manager._pending_hello_counts)
        finally:
            writer.close()
    finally:
        await _stop(manager)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    refusals = [m for m in warnings if str(declared) in m and str(MAX_HELLO_FRAME_BYTES) in m]
    assert len(refusals) == 1, warnings
    assert not any("PAYLOAD-MARKER" in m for m in warnings), "a refusal echoed the payload"


# --- phase 2: the frame before the proof, dial side ------------------------------


@pytest.mark.asyncio
async def test_a_dial_refuses_a_challenge_frame_larger_than_a_handshake_needs(tmp_path):
    """The far end passes the certificate check and then declares 5 MB.

    The dial's budget is 20 s; without the cap the read waits for the body
    until that budget runs out, and this test's 8 s wait fails first.
    """
    node_id, cert_file, key_file = _identity(tmp_path, "far_end")
    dialler = _manager(tmp_path, "dialler")
    release = asyncio.Event()

    async def _oversize_challenge(reader, writer):
        try:
            writer.write(b"0005000000" + b'{"command": "HELLO_CHALLENGE"')
            await writer.drain()
            await asyncio.wait_for(release.wait(), timeout=30)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server = await asyncio.start_server(_oversize_challenge, LOOPBACK, 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        started = time.monotonic()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(dialler.connect_directly(LOOPBACK, port, node_id, timeout=20.0), timeout=8)
        assert time.monotonic() - started < 3, "the dial waited for the declared body"
        assert node_id not in dialler.peers
    finally:
        release.set()
        server.close()
        await asyncio.wait_for(server.wait_closed(), timeout=5)


# --- after the proof: the honest handshake and the full cap ----------------------


@pytest.mark.asyncio
async def test_an_honest_hello_passes_and_a_proved_peer_may_then_send_a_megabyte(tmp_path, monkeypatch):
    """The real dial against the real listener, then 1 MB each way.

    1 MB is sixteen times the handshake cap and far under MAX_FRAME_BYTES:
    the small cap must end at HELLO_ACK on both sides.
    """
    listener = _manager(tmp_path, "listener")
    dialler = _manager(tmp_path, "dialler")
    port = await _listen(listener, monkeypatch)
    big = "x" * 1_000_000
    at_listener = asyncio.get_running_loop().create_future()
    at_dialler = asyncio.get_running_loop().create_future()

    async def _listener_got(node_id, message):
        if message.get("command") == "BIG" and not at_listener.done():
            at_listener.set_result((node_id, message))

    async def _dialler_got(node_id, message):
        if message.get("command") == "BIG" and not at_dialler.done():
            at_dialler.set_result((node_id, message))

    listener.on_message_received = _listener_got
    dialler.on_message_received = _dialler_got
    try:
        await dialler.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)
        await _until(lambda: dialler.node_id in listener.peers)
        assert listener._failed_hello_counts == {}
        assert listener._pending_hello_counts == {}

        message = {"command": "BIG", "payload": {"pad": big}}
        assert MAX_HELLO_FRAME_BYTES < len(json.dumps(message)) < MAX_FRAME_BYTES

        await dialler.peers[listener.node_id].send(message)
        sender, got = await asyncio.wait_for(at_listener, timeout=10)
        assert sender == dialler.node_id and got["payload"]["pad"] == big

        await listener.peers[dialler.node_id].send(message)
        sender, got = await asyncio.wait_for(at_dialler, timeout=10)
        assert sender == listener.node_id and got["payload"]["pad"] == big
    finally:
        await _stop(dialler, listener)


def test_the_handshake_cap_clears_a_real_hello_by_a_wide_margin():
    """The number beside MAX_HELLO_FRAME_BYTES, as an assertion.

    A HELLO built as connect_directly builds it — this generator's certificate,
    a 2048-bit RSA-PSS signature — with a 200-character display name.
    """
    import base64
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        node_id, cert_file, _ = _identity(Path(d), "measured")
        cert_pem = cert_file.read_text()
    hello = {
        "command": "HELLO",
        "payload": {
            "node_id": node_id,
            "name": "x" * 200,
            "cert_pem": cert_pem,
            "hello_preimage_version": "dptp-hello-v1",
            "hello_signature": base64.b64encode(os.urandom(256)).decode(),
        },
    }
    framed = 10 + len(json.dumps(hello).encode())

    assert framed < 2_000
    assert MAX_HELLO_FRAME_BYTES > 30 * framed
    assert MAX_HELLO_FRAME_BYTES < MAX_FRAME_BYTES
