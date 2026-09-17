"""A restored record leaves again with what its signature covers.

Written for `dptp-msg-v1`, where the preimage covered the tool calls
themselves: `import_history` rebuilt the stored record from a whitelist that
did not name them (Ark's review, 2026-09-06), so a verified record lost them
on the way in and the next node refused the re-export as tampered.

**ADR-042 changed what the signature covers**, and this file changed with it.
Under v2 the preimage covers the *digest* of the calls, the calls do not
travel at all, and what has to survive an import is the digest. The v1 case is
kept below rather than deleted: eleven records on this pair are still v1, and
for them stripping the calls is still a refusal.

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
from dpc_protocol.message_signing import digest_of_tool_calls, message_content_hash

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
    # The calls stay with their author; their digest is what travels.
    assert "tool_calls" not in exported[0]
    assert exported[0]["tool_calls_digest"] == digest_of_tool_calls(TOOL_CALLS)
    assert exported[1]["tool_calls_digest"] == ""
    return exported


def test_the_digest_survives_the_round_trip_through_a_restored_history(author):
    node_id, _ = author
    exported = _signed_export(author)

    receiver = _monitor("dpc-node-" + "b" * 32)
    receiver.import_history(exported)

    restored = receiver.get_message_history()
    assert [m["verification"] for m in restored] == ["verified", "verified"]
    assert "tool_calls" not in restored[0], "the calls reached a node that is not their owner"
    assert restored[0]["tool_calls_digest"] == digest_of_tool_calls(TOOL_CALLS)

    # The receiver hands the history on; the third node recomputes the hash.
    re_exported = receiver.export_history()
    assert "tool_calls" not in re_exported[0]
    assert re_exported[0]["tool_calls_digest"] == restored[0]["tool_calls_digest"]

    third = _monitor("dpc-node-" + "c" * 32)
    kept, verdict = third._verify_incoming(re_exported[0])
    assert verdict == "verified", verdict
    assert kept["signer_node_id"] == node_id


def test_a_restored_record_without_its_digest_is_what_the_next_node_refuses(author):
    """Named so the test above is not vacuous: the digest is load-bearing."""
    exported = _signed_export(author)
    stripped = dict(exported[0])
    del stripped["tool_calls_digest"]

    kept, verdict = _monitor("dpc-node-" + "c" * 32)._verify_incoming(stripped)

    assert kept is None
    assert verdict == "content does not match its hash"


def test_a_v1_record_still_needs_its_calls_to_verify(author):
    """The eleven records this pair carries are v1, and the old rule holds for them."""
    node_id, signer = author
    sender = _monitor(node_id, signer)
    sender.add_message(
        role="assistant", content="an answer", sender_node_id=node_id,
        sender_name="Ark", sender_type="agent", agent_owner=node_id,
        tool_calls=TOOL_CALLS,
    )
    record = dict(sender.get_message_history()[0])
    record["preimage_version"] = "dptp-msg-v1"
    record["content_hash"] = message_content_hash(
        conversation_id=ROOM, message_id=record["id"],
        sender_node_id=record["sender_node_id"], sender_name=record["sender_name"],
        sender_type=record["sender_type"], agent_owner=record["agent_owner"],
        timestamp=record["timestamp"], content=record["content"],
        tool_calls=TOOL_CALLS, version="dptp-msg-v1",
    )
    record["signature"] = signer.sign_commit(record["content_hash"])
    record.pop("tool_calls_digest", None)
    third = _monitor("dpc-node-" + "c" * 32)

    kept, verdict = third._verify_incoming(record)
    assert verdict == "verified", verdict

    without_calls = dict(record)
    del without_calls["tool_calls"]
    kept, verdict = third._verify_incoming(without_calls)
    assert kept is None
    assert verdict == "content does not match its hash"
