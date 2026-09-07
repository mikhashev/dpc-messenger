"""An agent's tool calls are signed with the post, not patched in afterwards.

`tool_calls` has been inside the signing preimage since 2026-08-06
(`dpc-protocol/dpc_protocol/message_signing.py`), and the receiver recomputes
the hash with them (`group_handler._authenticate_author`). The sender, though,
signed the record without them and set them on the stored record after the
signature was made — so the hash covered one message and the wire carried
another. Observed live on 2026-09-06: message 20cca18040f94a27 with eleven
tool calls, refused by the Linux node as "content does not match its hash".
Every agent post with tool calls had been refused by every peer for a month,
and the author's own export failed its own signature.

The sender here is the real `send_group_agent_message` bound to a stub
service, writing into a real `ConversationMonitor` with a real key; the
receiver is the real `GroupTextHandler` check. The collaborators are fake,
the code under test is not.
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
from dpc_protocol.message_signing import (
    PREIMAGE_VERSION,
    digest_of_tool_calls,
    message_content_hash,
)
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.group_handler import GroupTextHandler
from dpc_client_core.service import CoreService


GROUP = "group-1234"
RELAY = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
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
    return node_id, CommitSigner(node_id, key)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Service:
    """Only the collaborators are stubbed; the methods under test are real."""

    def __init__(self, node_id, signer, monkeypatch):
        self.node_id = node_id
        self.p2p_manager = type("P2P", (), {"node_id": node_id, "peers": {}})()
        self.group_manager = self
        self.local_api = _Api()
        self.broadcast = []
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        # A real monitor signing with a real key; the disk is left alone.
        monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
        self.monitor = ConversationMonitor(
            conversation_id=GROUP,
            participants=[{"node_id": node_id, "name": "self", "context": "local"}],
            llm_manager=None,
        )
        self.monitor._signer = signer

    # group_manager
    def get_group(self, group_id):
        if group_id != GROUP:
            return None
        return type("Group", (), {
            "group_id": GROUP, "members": [self.node_id], "name": "1234",
            "agents": {self.node_id: ["agent_001"]},
            "agent_names": {self.node_id: {"agent_001": "Ark"}},
        })()

    def _get_or_create_conversation_monitor(self, _):
        return self.monitor

    async def _broadcast_to_group(self, group_id, message):
        self.broadcast.append(message)

    # Token-meter and mention plumbing after the broadcast; not under test.
    llm_manager = type("LLM", (), {
        "get_context_window": lambda self, _: 128000,
        "get_active_model_name": lambda self: "m",
    })()

    async def _handle_group_agent_mentions(self, *a, **kw):
        pass

    def _worst_group_agent_context(self, group_id):
        return None

    def _group_agent_context_list(self, group_id):
        return []

    _signature_fields_for = staticmethod(CoreService._signature_fields_for)
    _names_this_node_may_post_as = CoreService._names_this_node_may_post_as

    def _get_agent_display_name(self, agent_id):
        return {"agent_001": "Ark"}.get(agent_id, agent_id)

    def get_cc_display_name(self):
        return "CC"


async def _post(author, monkeypatch, tool_calls=TOOL_CALLS):
    node_id, signer = author
    service = _Service(node_id, signer, monkeypatch)
    await CoreService.send_group_agent_message(service, GROUP, "Ark", "an answer", tool_calls=tool_calls)
    assert service.broadcast, "nothing was sent to the group"
    record = service.monitor.get_message_history()[-1]
    payload = service.broadcast[-1]["payload"]
    assert payload["message_id"] == record["id"]
    return record, payload


def _hash_of(record, tool_calls):
    return message_content_hash(
        conversation_id=GROUP, message_id=record["id"],
        sender_node_id=record["sender_node_id"], sender_name=record["sender_name"],
        sender_type=record["sender_type"], agent_owner=record["agent_owner"],
        timestamp=record["timestamp"], content=record["content"],
        tool_calls=tool_calls,
    )


@pytest.mark.asyncio
async def test_the_stored_record_is_signed_over_the_digest_of_its_tool_calls(author, monkeypatch):
    """v2: the author binds what its agent did, and keeps the doing to itself."""
    record, payload = await _post(author, monkeypatch)

    assert record["tool_calls"] == TOOL_CALLS
    assert record["tool_calls_digest"] == digest_of_tool_calls(TOOL_CALLS)
    assert record["content_hash"] == _hash_of(record, record["tool_calls"])
    # Not vacuous: a different set of calls is a different signature.
    assert record["content_hash"] != _hash_of(record, None)

    # The calls do not travel; their digest does (ADR-042).
    assert "tool_calls" not in payload
    assert payload["tool_calls_digest"] == record["tool_calls_digest"]


@pytest.mark.asyncio
async def test_a_peer_verifies_the_post_as_sent(author, monkeypatch):
    node_id, _ = author
    _, payload = await _post(author, monkeypatch)

    who, verdict, fields = GroupTextHandler(service=None)._authenticate_author(RELAY, payload)

    assert verdict == "verified", verdict
    assert who == node_id
    assert fields["content_hash"] == payload["content_hash"]


@pytest.mark.asyncio
async def test_a_post_without_tool_calls_still_verifies(author, monkeypatch):
    """None in the record, [] on the wire: the preimage spells both as empty."""
    node_id, _ = author
    record, payload = await _post(author, monkeypatch, tool_calls=None)

    assert "tool_calls" not in record
    assert "tool_calls" not in payload
    assert record["tool_calls_digest"] == ""
    _, verdict, _ = GroupTextHandler(service=None)._authenticate_author(RELAY, payload)
    assert verdict == "verified"


def test_a_hash_made_without_the_tool_calls_it_carries_is_refused(author):
    """What every peer saw for a month — and must keep refusing."""
    node_id, signer = author
    payload = {
        "group_id": GROUP, "text": "an answer", "sender_name": "Ark",
        "sender_type": "agent", "sender_node_id": node_id, "agent_owner": node_id,
        "message_id": "20cca18040f94a27", "timestamp": "2026-09-06T00:00:00+00:00",
        "mentions": [], "is_agent": True, "tool_calls": TOOL_CALLS,
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=payload["message_id"],
        sender_node_id=node_id, sender_name="Ark", sender_type="agent",
        agent_owner=node_id, timestamp=payload["timestamp"],
        content="an answer", tool_calls=None,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = node_id
    payload["preimage_version"] = PREIMAGE_VERSION

    who, verdict, fields = GroupTextHandler(service=None)._authenticate_author(RELAY, payload)

    assert verdict == "rejected"
    assert who == RELAY and fields is None


class _Receiver:
    """The far side: a real monitor under a temporary home, a real handler."""

    def __init__(self, node_id, author_node_id, monkeypatch):
        self.node_id = node_id
        self.p2p_manager = type("P2P", (), {"node_id": node_id, "peers": {}})()
        self.group_manager = self
        self.members = [node_id, author_node_id]
        self.local_api = _Api()
        self.conversation_monitors = {}
        self._processed_message_ids = set()
        self._max_processed_ids = 1000
        monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)

    def get_group(self, group_id):
        if group_id != GROUP:
            return None
        return type("Group", (), {"group_id": GROUP, "members": self.members, "agents": {}})()

    def _get_or_create_conversation_monitor(self, conversation_id):
        if conversation_id not in self.conversation_monitors:
            self.conversation_monitors[conversation_id] = ConversationMonitor(
                conversation_id=conversation_id,
                participants=[{"node_id": self.node_id, "name": "self", "context": "local"}],
                llm_manager=None,
            )
        return self.conversation_monitors[conversation_id]


@pytest.mark.asyncio
async def test_the_peers_stored_copy_carries_what_its_hash_covers(author, monkeypatch, tmp_path):
    """Received through the real handler, the copy must re-export and verify."""
    node_id, _ = author
    _, payload = await _post(author, monkeypatch)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path / "receiver"))
    receiver = _Receiver(RELAY, node_id, monkeypatch)

    await GroupTextHandler(service=receiver).handle(node_id, payload)

    monitor = receiver.conversation_monitors[GROUP]
    stored = monitor.get_message_history()[-1]
    assert stored["id"] == payload["message_id"]
    # The peer holds the digest and never the calls — that is the whole point.
    assert "tool_calls" not in stored
    assert stored["tool_calls_digest"] == digest_of_tool_calls(TOOL_CALLS)
    assert stored["content_hash"] == _hash_of(stored, TOOL_CALLS)
    assert stored["signer_node_id"] == node_id

    # A third node receiving this history recomputes over the export.
    exported = monitor.export_history()[-1]
    assert "tool_calls" not in exported
    assert exported["tool_calls_digest"] == stored["tool_calls_digest"]
    kept, verdict = monitor._verify_incoming(exported)
    assert verdict == "verified", verdict
