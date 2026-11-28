"""A live record's verdict is stored, not only broadcast.

The live group path computed a verdict in `_authenticate_author`, handed it to
the UI broadcast and dropped it: the record on disk said nothing about its own
check, and after a reload the readers' "legacy" default stood in for a verdict
that had been reached and lost (the older card
THE-VERDICT-OF-A-LIVE-RECORD-IS-BROADCAST-AND-NEVER-STORED). Now the handler
puts the verdict on the ConvMessage beside the signature fields, `on_message`
carries it into `add_message`, and the stored record keeps it.

The receiver here is the real `GroupTextHandler.handle` over a stub service
with a real `ConversationMonitor` — the seam the neighbouring handler tests
use. A "legacy" post is the trap: it arrives with no signature fields, which
is also what this node's own records look like, and it must not be stamped
"own" for that.
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
from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from dpc_client_core.conversation_monitor import ConversationMonitor, Message
from dpc_client_core.message_handlers.group_handler import GroupTextHandler


GROUP = "group-1234"
RELAY = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Receiver:
    """The far side: a real monitor, a real handler; stubbed collaborators."""

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
        # A legacy arrival has no signature fields, so the receiver mints a
        # hash and would reach for the machine's own key: nothing here reads a
        # signature, and ~/.dpc stays untouched.
        monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: None)

    def get_group(self, group_id):
        if group_id != GROUP:
            return None
        return type("Group", (), {"group_id": GROUP, "members": self.members,
                                  "agents": {}})()

    def _get_or_create_conversation_monitor(self, conversation_id):
        if conversation_id not in self.conversation_monitors:
            self.conversation_monitors[conversation_id] = ConversationMonitor(
                conversation_id=conversation_id,
                participants=[{"node_id": self.node_id, "name": "self", "context": "local"}],
                llm_manager=None,
            )
        return self.conversation_monitors[conversation_id]


def _author_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    node_id = generate_node_id(key.public_key())
    return node_id, key


def _cert_bytes(node_id, key):
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
    return cert.public_bytes(serialization.Encoding.PEM)


def _payload(node_id, signer, text="no mentions here"):
    """A GROUP_TEXT as the author's node would send it."""
    payload = {
        "group_id": GROUP, "text": text, "sender_name": "Mike (linux)",
        "sender_type": "human", "sender_node_id": node_id, "agent_owner": None,
        "message_id": "m-live-1", "timestamp": "2026-11-28T00:00:00+00:00",
        "mentions": [],
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=payload["message_id"],
        sender_node_id=node_id, sender_name="Mike (linux)", sender_type="human",
        agent_owner=None, timestamp=payload["timestamp"], content=text,
        tool_calls=None,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = node_id
    payload["preimage_version"] = PREIMAGE_VERSION
    return payload


async def _receive(payload, author_node_id, monkeypatch, tmp_path):
    """One GROUP_TEXT through the real handler; the stored record and the broadcast."""
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    receiver = _Receiver(RELAY, author_node_id, monkeypatch)
    await GroupTextHandler(service=receiver).handle(author_node_id, payload)
    monitor = receiver.conversation_monitors[GROUP]
    return monitor.get_message_history()[-1], receiver.local_api.events


@pytest.mark.asyncio
async def test_a_verified_post_is_stored_with_its_verdict(tmp_path, monkeypatch):
    node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir()
    (tmp_path / "peers" / f"{node_id}.crt").write_bytes(_cert_bytes(node_id, key))

    stored, events = await _receive(_payload(node_id, CommitSigner(node_id, key)),
                                    node_id, monkeypatch, tmp_path)

    assert stored["verification"] == "verified"
    # The broadcast to the UI is unchanged.
    assert [p["verification"] for _, p in events if _ == "group_text_received"] == ["verified"]


@pytest.mark.asyncio
async def test_an_unverified_post_is_stored_with_its_verdict(tmp_path, monkeypatch):
    """Signed and honest, but the certificate is not cached here (yet)."""
    node_id, key = _author_key()

    stored, _ = await _receive(_payload(node_id, CommitSigner(node_id, key)),
                               node_id, monkeypatch, tmp_path)

    assert stored["verification"] == "unverified"
    assert stored["verification"] != "own"


@pytest.mark.asyncio
async def test_a_legacy_post_is_stored_legacy_never_own(tmp_path, monkeypatch):
    """No signature fields: also what this node's own records look like."""
    node_id, key = _author_key()
    payload = _payload(node_id, CommitSigner(node_id, key))
    for field in ("content_hash", "signature", "signer_node_id", "preimage_version"):
        del payload[field]

    stored, _ = await _receive(payload, node_id, monkeypatch, tmp_path)

    assert stored["verification"] == "legacy"
    assert stored["verification"] != "own"


@pytest.mark.asyncio
async def test_the_local_send_path_stores_own(tmp_path, monkeypatch):
    """send_group_message and the agent send both feed on_message a ConvMessage
    with no verdict and no signature fields: this node wrote the record."""
    monkeypatch.setattr(ConversationMonitor, "save_history", lambda self: True)
    monitor = ConversationMonitor(
        conversation_id=GROUP,
        participants=[{"node_id": RELAY, "name": "self", "context": "local"}],
        llm_manager=None,
    )
    monitor._get_signer = lambda: None

    await monitor.on_message(Message(
        message_id="m-own-1", conversation_id=GROUP, sender_node_id=RELAY,
        sender_name="Mike", text="mine", timestamp="2026-11-28T00:00:00+00:00",
    ))

    assert monitor.message_history[-1]["verification"] == "own"
