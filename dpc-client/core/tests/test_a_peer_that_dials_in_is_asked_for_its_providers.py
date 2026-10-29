"""A peer that dials in is asked for its AI providers, as one we dial is.

The outgoing TLS path and the WebRTC path send GET_PROVIDERS right after the
connection is set up; the inbound handshake did not, so a peer that connected
to us showed only our own models in the Text list (the PROVIDERS_RESPONSE never
came). The inbound handshake now asks once the peer is accepted and its
listener runs. A rejected HELLO is hung up on and is sent nothing.

Same harness as the failed-HELLO test: loopback TLS through asyncio.start_server,
every file in tmp_path.
"""

import asyncio
import importlib.util
from pathlib import Path

import pytest

# The neighbour's harness, loaded by path: tests/ is not a package.
_spec = importlib.util.spec_from_file_location(
    "_failed_hello_harness",
    Path(__file__).with_name("test_a_failed_hello_leaves_the_peer_it_names_connected.py"),
)
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
_Identity, _finish, _hello = _harness._Identity, _harness._finish, _harness._hello
_manager, _next, _serve, _until = _harness._manager, _harness._next, _harness._serve, _harness._until


@pytest.mark.asyncio
async def test_an_accepted_inbound_hello_is_followed_by_a_get_providers(tmp_path):
    manager = _manager(tmp_path)
    server, port = await _serve(manager)
    who = _Identity()
    writer = None
    try:
        reader, writer, answer = await _hello(
            port, claims=who.node_id, cert_pem=who.cert_pem, signer=who
        )
        assert answer["command"] == "HELLO_ACK"
        frame = await asyncio.wait_for(_next(reader), timeout=5)
        assert frame is not None and frame["command"] == "GET_PROVIDERS"
        await _until(lambda: who.node_id in manager.peers)
    finally:
        await _finish(server, manager, *([writer] if writer else []))


@pytest.mark.asyncio
async def test_a_rejected_inbound_hello_is_sent_no_get_providers(tmp_path):
    manager = _manager(tmp_path)
    server, port = await _serve(manager)
    victim, attacker = _Identity(), _Identity()
    writer = None
    try:
        reader, writer, answer = await _hello(
            port, claims=victim.node_id, cert_pem=attacker.cert_pem, signer=attacker
        )
        assert answer is None, answer  # hung up, no HELLO_ACK and nothing after it
        assert victim.node_id not in manager.peers
    finally:
        await _finish(server, manager, *([writer] if writer else []))
