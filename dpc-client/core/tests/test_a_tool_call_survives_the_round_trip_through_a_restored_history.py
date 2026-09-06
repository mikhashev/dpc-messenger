"""A record restored through `import_history` leaves again with its tool calls.

`tool_calls` is inside the signing preimage, `_verify_incoming` recomputes
the hash with them, and `export_history` ships them — but `import_history`
rebuilt the stored record from a whitelist of fields that did not name them
(Ark's review, 2026-09-06, a hole left by 28ecd67a). A verified record lost
its tool calls on the way into the receiver's history; re-exported from
there, it reached the next node without them, whose recomputation with
`None` refused it as tampered. A group history restored once could never be
handed on.

Three real monitors, one real key, one real certificate: sender signs,
receiver restores, a third node verifies the receiver's export.
"""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_client_core.conversation_monitor import ConversationMonitor

ROOM = "group-round-trip"
TOOL_CALLS = [
    {"name": "run_shell", "input": {"command": "git status"}, "output": "clean"},
    {"name": "read_file", "input": {"path": "README.md"}, "output": "# D-PC"},
]


@pytest.fixture
def author(tmp_path, monkeypatch):
    """A real key, a real certificate, cached where verification looks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    peers = tmp_path / "peers"
    peers.mkdir()
    (peers / f"{node_id}.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    return node_id, CommitSigner(node_id, key)


def _monitor(node_id, signer=None):
    m = ConversationMonitor(
        conversation_id=ROOM,
        participants=[{"node_id": node_id, "name": "self", "context": "local"}],
        llm_manager=None,
    )
    # A monitor with no key of its own; only the sender signs.
    m._signer = signer
    m._get_signer = lambda: signer
    return m


def _signed_export(author):
    node_id, signer = author
    sender = _monitor(node_id, signer)
    sender.add_message(
        role="assistant", content="an answer", sender_node_id=node_id,
        sender_name="Ark", sender_type="agent", agent_owner=node_id,
        tool_calls=TOOL_CALLS,
    )
    sender.add_message(
        role="assistant", content="no tools this time", sender_node_id=node_id,
        sender_name="Ark", sender_type="agent", agent_owner=node_id,
    )
    exported = sender.export_history()
    assert exported[0]["tool_calls"] == TOOL_CALLS
    assert "tool_calls" not in exported[1]
    return exported


def test_a_tool_call_survives_the_round_trip_through_a_restored_history(author):
    node_id, _ = author
    exported = _signed_export(author)

    receiver = _monitor("dpc-node-" + "b" * 32)
    receiver.import_history(exported)

    restored = receiver.get_message_history()
    assert [m["verification"] for m in restored] == ["verified", "verified"]
    assert restored[0]["tool_calls"] == TOOL_CALLS
    assert "tool_calls" not in restored[1], "empty stays absent, as add_message stores it"

    # The receiver hands the history on; the third node recomputes the hash.
    re_exported = receiver.export_history()
    assert re_exported[0]["tool_calls"] == TOOL_CALLS

    third = _monitor("dpc-node-" + "c" * 32)
    kept, verdict = third._verify_incoming(re_exported[0])
    assert verdict == "verified", verdict
    assert kept["tool_calls"] == TOOL_CALLS
    assert kept["signer_node_id"] == node_id


def test_a_restored_history_without_its_tool_calls_is_what_the_next_node_refuses(author):
    """The old behaviour, named so the test above is not vacuous."""
    exported = _signed_export(author)
    stripped = dict(exported[0])
    del stripped["tool_calls"]

    kept, verdict = _monitor("dpc-node-" + "c" * 32)._verify_incoming(stripped)

    assert kept is None
    assert verdict == "content does not match its hash"
