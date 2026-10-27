"""A HELLO signature names both ends, and never the bytes a listener chose.

The dialler used to sign the listener's HELLO_CHALLENGE nonce as it arrived,
with node.key and the RSA-PSS parameters `CommitSigner.sign_commit` uses. Two
defects came of it. The signature named nobody, so a node M we dialled could
dial T itself, hand T's nonce to us and our HELLO to T, and be us at T. And the
nonce was any length, so M could send a content hash as the nonce and get our
signature over it: a group message, a vote, a gossip origin in our name.

Now the challenge is exactly HELLO_NONCE_BYTES and carries
`hello_preimage_version`; the dialler signs `hello_content_hash(nonce, the
listener it proved over TLS, itself)` and the listener rebuilds it with its own
id. Hard cut: the old shape is refused in both directions.

Cross-platform: loopback TLS through the real `P2PManager.start_server` and
`connect_directly`; no socket options, no signals; every file in tmp_path,
never ~/.dpc.
"""

import asyncio
import base64
import datetime
import hashlib
import os
import ssl
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_protocol.message_signing import HELLO_PREIMAGE_VERSION, hello_content_hash
from dpc_protocol.protocol import read_message, write_message
from dpc_client_core import p2p_manager as p2p_module
from dpc_client_core.own_addresses import OwnAddresses
from dpc_client_core.p2p_manager import HELLO_NONCE_BYTES, P2PManager
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


def _key(manager):
    return serialization.load_pem_private_key(manager.key_file.read_bytes(), password=None)


def _manager(tmp_path: Path, name: str) -> P2PManager:
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
    manager._hello_timeout = 10.0
    manager._max_pending_hellos_per_ip = 8
    return manager


async def _nothing(*args, **kwargs):
    return None


async def _listen(manager, monkeypatch):
    """The real `start_server` on a free loopback port; returns the port."""
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


def _client_ctx():
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _server_ctx(cert_file, key_file):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    return ctx


async def _next(reader):
    """The next frame, or None once the far end has hung up either way."""
    try:
        return await asyncio.wait_for(read_message(reader), timeout=5)
    except (ConnectionError, asyncio.IncompleteReadError):
        return None


async def _until(condition, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"not true within {timeout}s"
        await asyncio.sleep(0.02)


async def _close_server(server):
    server.close()
    await asyncio.wait_for(server.wait_closed(), timeout=5)


# --- the relay -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hello_signature_made_for_one_listener_is_refused_by_another(tmp_path, monkeypatch):
    """V dials M; M dials T, hands T's challenge to V and V's HELLO to T.

    The HELLO is V's own and verifies at M — the relay holds a genuine
    signature — and T refuses it because the preimage names M, not T.
    """
    victim = _manager(tmp_path, "victim")
    target = _manager(tmp_path, "target")
    target_port = await _listen(target, monkeypatch)
    relay_id, relay_cert, relay_key = _identity(tmp_path, "relay")
    seen = {}

    async def _relay(v_reader, v_writer):
        t_reader, t_writer = await asyncio.open_connection(LOOPBACK, target_port, ssl=_client_ctx())
        try:
            challenge = await _next(t_reader)
            seen["challenge"] = challenge
            await write_message(v_writer, challenge)
            hello = await _next(v_reader)
            seen["hello"] = hello
            await write_message(t_writer, hello)
            seen["target_answer"] = await _next(t_reader)
        finally:
            t_writer.close()
            v_writer.close()

    relay = await asyncio.start_server(_relay, LOOPBACK, 0, ssl=_server_ctx(relay_cert, relay_key))
    relay_port = relay.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ConnectionError):
            await victim.connect_directly(LOOPBACK, relay_port, relay_id, timeout=15.0)
        await _until(lambda: "target_answer" in seen)

        hello = seen["hello"]["payload"]
        nonce = base64.b64decode(seen["challenge"]["payload"]["nonce"])
        victim_public = _key(victim).public_key()
        made_for_relay = hello_content_hash(
            nonce=nonce, listener_node_id=relay_id, dialer_node_id=victim.node_id
        )
        assert CommitSigner.verify_with_public_key(
            victim_public, made_for_relay, hello["hello_signature"]
        ), "the relay should hold the victim's genuine signature, or the test proves nothing"

        assert seen["target_answer"] is None, f"T answered a relayed HELLO: {seen['target_answer']}"
        await _until(lambda: target._failed_hello_counts)
        assert victim.node_id not in target.peers
    finally:
        await _close_server(relay)
        await _stop(victim, target)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["a_dials_b", "b_dials_a"])
async def test_an_honest_dial_still_passes_in_either_direction(tmp_path, monkeypatch, direction):
    a = _manager(tmp_path, "a")
    b = _manager(tmp_path, "b")
    a_port = await _listen(a, monkeypatch)
    b_port = await _listen(b, monkeypatch)
    dialler, listener, port = (a, b, b_port) if direction == "a_dials_b" else (b, a, a_port)
    try:
        await dialler.connect_directly(LOOPBACK, port, listener.node_id, timeout=15.0)
        await _until(lambda: dialler.node_id in listener.peers)
        assert listener.node_id in dialler.peers
        assert listener._failed_hello_counts == {}
    finally:
        await _stop(a, b)


# --- the signing oracle ------------------------------------------------------------


async def _fake_listener(tmp_path, name, challenge_payload):
    """A listener holding a real identity that sends `challenge_payload`
    and keeps whatever the dialler answers with."""
    node_id, cert_file, key_file = _identity(tmp_path, name)
    got = asyncio.get_running_loop().create_future()

    async def _handle(reader, writer):
        try:
            await write_message(writer, {"command": "HELLO_CHALLENGE", "payload": challenge_payload})
            answer = await _next(reader)
            if not got.done():
                got.set_result(answer)
        finally:
            writer.close()

    server = await asyncio.start_server(_handle, LOOPBACK, 0, ssl=_server_ctx(cert_file, key_file))
    return node_id, server, server.sockets[0].getsockname()[1], got


_A_HEX_HASH = hashlib.sha256(b"a group message the attacker wants signed").hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "nonce_b64",
    [
        base64.b64encode(_A_HEX_HASH.encode()).decode(),        # a whole hex hash, 64 bytes
        base64.b64encode(os.urandom(HELLO_NONCE_BYTES - 1)).decode(),
        base64.b64encode(os.urandom(HELLO_NONCE_BYTES + 1)).decode(),
        base64.b64encode(b"").decode(),
        "not base64 at all!",
        None,
    ],
    ids=["hex_hash_64", "31_bytes", "33_bytes", "empty", "not_base64", "absent"],
)
async def test_the_dialler_signs_nothing_for_a_nonce_that_is_not_32_bytes(tmp_path, nonce_b64):
    dialler = _manager(tmp_path, "dialler")
    payload = {"hello_preimage_version": HELLO_PREIMAGE_VERSION}
    if nonce_b64 is not None:
        payload["nonce"] = nonce_b64
    listener_id, server, port, got = await _fake_listener(tmp_path, "listener", payload)
    try:
        with pytest.raises(ConnectionError) as err:
            await dialler.connect_directly(LOOPBACK, port, listener_id, timeout=15.0)
        assert f"not {HELLO_NONCE_BYTES} bytes" in str(err.value), str(err.value)
        assert await asyncio.wait_for(got, timeout=5) is None, "the dialler sent a HELLO"
    finally:
        await _close_server(server)


@pytest.mark.asyncio
async def test_a_32_byte_nonce_a_listener_chose_never_comes_back_as_a_signature_over_its_hash(tmp_path):
    """The nonce is the first 32 bytes of a hex content hash, the most a
    32-byte nonce can spell of one. Neither it nor the whole hash verifies
    under CommitSigner's verifier; only the HELLO preimage does."""
    dialler = _manager(tmp_path, "dialler")
    nonce = _A_HEX_HASH[:HELLO_NONCE_BYTES].encode()
    assert len(nonce) == HELLO_NONCE_BYTES
    listener_id, server, port, got = await _fake_listener(
        tmp_path,
        "listener",
        {"nonce": base64.b64encode(nonce).decode(), "hello_preimage_version": HELLO_PREIMAGE_VERSION},
    )
    try:
        with pytest.raises(ConnectionError):
            await dialler.connect_directly(LOOPBACK, port, listener_id, timeout=15.0)
        hello = await asyncio.wait_for(got, timeout=5)
    finally:
        await _close_server(server)

    assert hello and hello["command"] == "HELLO"
    payload = hello["payload"]
    assert "nonce_signature" not in payload
    assert payload["hello_preimage_version"] == HELLO_PREIMAGE_VERSION
    signature = payload["hello_signature"]
    public = _key(dialler).public_key()

    assert CommitSigner.verify_with_public_key(
        public,
        hello_content_hash(nonce=nonce, listener_node_id=listener_id, dialer_node_id=dialler.node_id),
        signature,
    )
    assert not CommitSigner.verify_with_public_key(public, _A_HEX_HASH, signature)
    assert not CommitSigner.verify_with_public_key(public, _A_HEX_HASH[:HELLO_NONCE_BYTES], signature)


# --- the old shape, refused both ways ---------------------------------------------


def _raw_nonce_signature(key, nonce: bytes) -> str:
    """What a pre-1.11 dialler sent as `nonce_signature`."""
    sig = key.sign(
        nonce,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["nonce_signature_only", "hello_signature_without_version"])
async def test_the_listener_refuses_a_hello_of_the_old_shape(tmp_path, monkeypatch, caplog, shape):
    listener = _manager(tmp_path, "listener")
    port = await _listen(listener, monkeypatch)
    old_id, old_cert, old_key_file = _identity(tmp_path, "old")
    old_key = serialization.load_pem_private_key(old_key_file.read_bytes(), password=None)
    reader, writer = await asyncio.open_connection(LOOPBACK, port, ssl=_client_ctx())
    try:
        challenge = await _next(reader)
        assert challenge["payload"]["hello_preimage_version"] == HELLO_PREIMAGE_VERSION
        nonce = base64.b64decode(challenge["payload"]["nonce"])
        assert len(nonce) == HELLO_NONCE_BYTES
        payload = {"node_id": old_id, "name": "old", "cert_pem": old_cert.read_text()}
        if shape == "nonce_signature_only":
            payload["nonce_signature"] = _raw_nonce_signature(old_key, nonce)
        else:
            # A signature that would verify, sent without the version it was made under.
            payload["hello_signature"] = CommitSigner(old_id, old_key).sign_commit(
                hello_content_hash(
                    nonce=nonce, listener_node_id=listener.node_id, dialer_node_id=old_id
                )
            )
        await write_message(writer, {"command": "HELLO", "payload": payload})
        assert await _next(reader) is None, "the listener answered an old-shape HELLO"
        await _until(lambda: listener._failed_hello_counts)
        assert old_id not in listener.peers
        assert "outdated version" in caplog.text
    finally:
        writer.close()
        await _stop(listener)


@pytest.mark.asyncio
async def test_the_dialler_refuses_a_challenge_without_a_preimage_version(tmp_path):
    """A pre-1.11 listener: a proper 32-byte nonce and no version. Nothing is signed."""
    dialler = _manager(tmp_path, "dialler")
    listener_id, server, port, got = await _fake_listener(
        tmp_path, "listener", {"nonce": base64.b64encode(os.urandom(HELLO_NONCE_BYTES)).decode()}
    )
    try:
        with pytest.raises(ConnectionError) as err:
            await dialler.connect_directly(LOOPBACK, port, listener_id, timeout=15.0)
        assert "outdated version" in str(err.value), str(err.value)
        assert await asyncio.wait_for(got, timeout=5) is None, "the dialler sent a HELLO"
    finally:
        await _close_server(server)
