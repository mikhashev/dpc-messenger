"""Dialling out, a Common Name is a claim and the public key is the proof.

Inbound, `_verify_hello_identity` runs three checks — CN, key fingerprint, and
a signature over a fresh nonce. Outbound, `_validate_peer_certificate` compared
the CN and stopped, so whoever controlled the address a node dialled could
terminate TLS with a self-signed certificate carrying the expected CN and be
that peer for the rest of the session. Every instrument keyed on
`sender_node_id` — firewall admission, the ledger's caller, the quota — sits
downstream of that.

The pair that matters is the live one: an honest certificate must reach the
certificate store, a forged one must not, and neither assertion reads a log
line to find out.
"""

import asyncio
import datetime
import ssl
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol.crypto import generate_node_id
from dpc_client_core.p2p_manager import P2PManager


def _identity(cn=None):
    """A node identity: key, node_id derived from it, certificate, and PEMs."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn or node_id)])
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
    return node_id, cert, cert_pem, key_pem


def _manager():
    """A P2PManager without touching ~/.dpc — only the cert path is exercised."""
    return P2PManager.__new__(P2PManager)


# --- the predicate -----------------------------------------------------------


def test_a_certificate_whose_key_hashes_to_the_node_id_validates():
    """Non-regression: an honest peer must still get through."""
    node_id, cert, _, _ = _identity()

    assert _manager()._validate_peer_certificate(cert, node_id) is True


def test_a_certificate_with_the_right_name_and_a_foreign_key_is_refused():
    """The attack: the name is copied, the key cannot be."""
    victim_node_id, _, _, _ = _identity()
    _, attacker_cert, _, _ = _identity(cn=victim_node_id)

    assert _manager()._validate_peer_certificate(attacker_cert, victim_node_id) is False


def test_a_certificate_for_a_different_peer_is_still_refused_by_the_name():
    """The older check keeps its job: a wrong peer fails on the CN."""
    _, other_cert, _, _ = _identity()
    expected_node_id, _, _, _ = _identity()

    assert _manager()._validate_peer_certificate(other_cert, expected_node_id) is False


# --- the live dial -----------------------------------------------------------


async def _tls_server(cert_pem, key_pem, tmp_path, name):
    """A server that completes TLS and then hangs up.

    Hanging up is deliberate. The dial fails either way once the certificate
    stage is behind it — an honest peer here never sends HELLO_CHALLENGE — so
    what separates the two worlds is not whether it fails but whether the
    certificate got as far as the store. It used to close rather than idle for
    a second reason — the dial's timeout covered the TLS connect and not the
    challenge read, so a silent server hung for ever. That is now bounded, and
    the test below is the one that holds it.
    """
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
    port = server.sockets[0].getsockname()[1]
    return server, port


async def _dial(manager, port, target_node_id):
    """Dial the local server and return what the store was offered."""
    offered = []
    manager._persist_peer_certificate = lambda nid, pem: (offered.append((nid, pem)), True)[1]

    with pytest.raises(ConnectionError):
        await manager.connect_directly("127.0.0.1", port, target_node_id, timeout=5.0)

    return offered


@pytest.mark.asyncio
async def test_dialling_a_peer_that_presents_a_foreign_key_never_reaches_the_store(tmp_path):
    """The falsifier from the board entry, run rather than reasoned."""
    victim_node_id, _, _, _ = _identity()
    _, _, attacker_cert_pem, attacker_key_pem = _identity(cn=victim_node_id)

    server, port = await _tls_server(attacker_cert_pem, attacker_key_pem, tmp_path, "attacker")
    try:
        offered = await _dial(_manager(), port, victim_node_id)
    finally:
        server.close()
        await server.wait_closed()

    assert offered == []


@pytest.mark.asyncio
async def test_dialling_an_honest_peer_reaches_the_store(tmp_path):
    """The other half: the guard must not amputate the working path."""
    node_id, _, cert_pem, key_pem = _identity()

    server, port = await _tls_server(cert_pem, key_pem, tmp_path, "honest")
    try:
        offered = await _dial(_manager(), port, node_id)
    finally:
        server.close()
        await server.wait_closed()

    assert len(offered) == 1
    assert offered[0][0] == node_id


async def _silent_tls_server(cert_pem, key_pem, tmp_path, name):
    """Completes TLS and then says nothing at all — the shape that hung."""
    cert_file = tmp_path / f"{name}.crt"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_text(cert_pem, encoding="utf-8")
    key_file.write_text(key_pem, encoding="utf-8")

    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    idle = asyncio.Event()

    async def _handle(reader, writer):
        await idle.wait()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=ctx)
    return server, server.sockets[0].getsockname()[1], idle


@pytest.mark.asyncio
async def test_a_peer_that_completes_tls_and_then_says_nothing_does_not_hold_the_dial(tmp_path):
    """The timeout the caller passes has to buy the exchange, not only the connect."""
    node_id, _, cert_pem, key_pem = _identity()
    manager = _manager()
    manager._persist_peer_certificate = lambda nid, pem: True

    server, port, idle = await _silent_tls_server(cert_pem, key_pem, tmp_path, "silent")
    started = time.monotonic()
    try:
        with pytest.raises(ConnectionError) as err:
            await asyncio.wait_for(
                manager.connect_directly("127.0.0.1", port, node_id, timeout=1.0),
                timeout=20,
            )
    finally:
        idle.set()
        server.close()
        await server.wait_closed()

    assert "HELLO_CHALLENGE" in str(err.value), str(err.value)
    assert time.monotonic() - started < 10, "the dial outlived the budget it was given"


@pytest.mark.asyncio
async def test_the_whole_dial_fits_the_budget_the_caller_paid_for(tmp_path):
    """The pre-flight is a TCP connect too, and it used to sit outside the clock.

    Both outside reviewers measured the same thing: a caller passing five
    seconds could spend five on the pre-flight and five more on the rest. On
    loopback the pre-flight is instant, so it is slowed here deliberately —
    that is the only way the two budgets show up as one number.
    """
    node_id, _, cert_pem, key_pem = _identity()
    manager = _manager()
    manager._persist_peer_certificate = lambda nid, pem: True

    async def _slow_preflight(host, port, timeout):
        await asyncio.sleep(min(1.5, timeout))
        return True, "ok"

    manager.test_port_connectivity = _slow_preflight

    server, port, idle = await _silent_tls_server(cert_pem, key_pem, tmp_path, "budgeted")
    started = time.monotonic()
    try:
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(
                manager.connect_directly("127.0.0.1", port, node_id, timeout=2.0),
                timeout=30,
            )
    finally:
        idle.set()
        server.close()
        await server.wait_closed()

    spent = time.monotonic() - started
    assert spent < 2.0 + 0.9, f"the dial spent {spent:.1f}s against a 2.0s budget"
