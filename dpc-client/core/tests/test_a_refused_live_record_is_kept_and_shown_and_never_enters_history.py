"""A refused live record is kept and shown, and never enters the history.

The live group path (`GroupTextHandler.handle`) refused a bad signature by
returning None: the record was gone, and only a log line said it had ever
arrived. Now every refusal is also kept in the rejected-records store as one
row and told to the open chat as one `group_record_rejected` event, while the
record itself still never enters `message_history` and nothing is answered to
the sender (the earlier card THE-VERDICT-OF-A-LIVE-RECORD-IS-BROADCAST-AND-
NEVER-STORED was the same gap for the verdict).

The receiver here is the real handler over a stub service with a real
`ConversationMonitor` — the seam the neighbouring handler tests use — with the
store pointed at the test's tmp_path.
"""

import datetime
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.crypto import generate_node_id
from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from dpc_client_core import rejected_records
from dpc_client_core.conversation_monitor import ConversationMonitor
from dpc_client_core.message_handlers.group_handler import GroupTextHandler


GROUP = "group-1234"
RELAY = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
TIMESTAMP = "2026-11-28T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    """The process-wide store, on the test's own folder, for this test only."""
    installed = rejected_records.RejectedRecordStore(
        tmp_path / "rejected-records" / rejected_records.FILE_NAME)
    rejected_records.set_store(installed)
    yield installed
    rejected_records.set_store(None)


class _Api:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _P2P:
    """The transport: nothing is ever answered back, and the test can see it."""

    def __init__(self, node_id):
        self.node_id = node_id
        self.peers = {}
        self.sent = []

    async def send_message_to_peer(self, node_id, message):
        self.sent.append((node_id, message))


class _Receiver:
    """The far side: a real monitor, a real handler; stubbed collaborators."""

    def __init__(self, node_id, author_node_id, monkeypatch):
        self.node_id = node_id
        self.p2p_manager = _P2P(node_id)
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


def _signed_payload(author_node_id, signer, text="no mentions here", **overrides):
    """A GROUP_TEXT as the author's node would send it."""
    payload = {
        "group_id": GROUP, "text": text, "sender_name": "Mike (linux)",
        "sender_type": "human", "sender_node_id": author_node_id, "agent_owner": None,
        "message_id": "m-live-1", "timestamp": TIMESTAMP,
        "mentions": [],
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=payload["message_id"],
        sender_node_id=author_node_id, sender_name="Mike (linux)", sender_type="human",
        agent_owner=None, timestamp=TIMESTAMP, content=text,
        tool_calls=None,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = author_node_id
    payload["preimage_version"] = PREIMAGE_VERSION
    payload.update(overrides)
    return payload


async def _receive(payload, transport_node_id, author_node_id, monkeypatch, tmp_path):
    """One GROUP_TEXT through the real handler; the receiver and its wire."""
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    receiver = _Receiver(RELAY, author_node_id, monkeypatch)
    # Held before the arrival, as a group this node keeps always is: the
    # assertion below reads what the refusal left in it.
    receiver._get_or_create_conversation_monitor(GROUP)
    await GroupTextHandler(service=receiver).handle(transport_node_id, payload)
    return receiver


def _one_row(store):
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1, f"exactly one store row, not {len(rows)}"
    return rows[0]


def _rejected_events(receiver):
    return [payload for name, payload in receiver.local_api.events
            if name == rejected_records.UI_EVENT]


def _by_way_of_signer_mismatch(author_node_id, key):
    payload = _signed_payload(author_node_id, CommitSigner(author_node_id, key))
    payload["sender_node_id"] = RELAY  # claims the relay wrote it
    return payload, RELAY


def _by_way_of_hash_mismatch(author_node_id, key):
    payload = _signed_payload(author_node_id, CommitSigner(author_node_id, key))
    payload["text"] = "transfer denied"  # hash and signature left intact
    return payload, RELAY


def _by_way_of_a_raising_check(author_node_id, key):
    return _signed_payload(author_node_id, CommitSigner(author_node_id, key)), RELAY


def _by_way_of_an_invalid_signature(author_node_id, key):
    other_id, other_key = _author_key()
    payload = _signed_payload(author_node_id, CommitSigner(author_node_id, key))
    # Signed by somebody else's key over the correct hash: the author's cached
    # certificate makes the check fail rather than say "cannot check yet".
    payload["signature"] = CommitSigner(other_id, other_key).sign_commit(payload["content_hash"])
    return payload, RELAY


@pytest.mark.asyncio
async def test_each_way_of_refusing_a_live_record_keeps_one_row_and_one_event(
        tmp_path, monkeypatch, store):
    # (how the refusal is made, the reason code, what the record claims about
    # its author — the signer-mismatch way claims the relay wrote it).
    ways = [
        (_by_way_of_signer_mismatch, "signer_mismatch", "the relay"),
        (_by_way_of_hash_mismatch, "content_hash_mismatch", "the author"),
        (_by_way_of_an_invalid_signature, "invalid_signature", "the author"),
    ]
    for build, expected_reason, claim in ways:
        author_node_id, key = _author_key()
        monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
        (tmp_path / "peers").mkdir(exist_ok=True)
        (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(
            _cert_bytes(author_node_id, key))
        # One fresh store per way: each refusal is judged on its own rows.
        fresh = rejected_records.RejectedRecordStore(
            tmp_path / "stores" / f"{expected_reason}.json")
        rejected_records.set_store(fresh)
        payload, transport = build(author_node_id, key)
        receiver = await _receive(payload, transport, author_node_id, monkeypatch, tmp_path)

        row = _one_row(fresh)
        assert row["reason"] == expected_reason
        assert row["live_count"] == 1 and row["sync_count"] == 0
        assert row["transport_node_id"] == RELAY
        assert row["claimed_sender_node_id"] == (
            RELAY if claim == "the relay" else author_node_id)
        assert row["record_id"] == "m-live-1"
        assert row["text"] == payload["text"]
        events = _rejected_events(receiver)
        assert len(events) == 1, f"{expected_reason}: one UI event, not {len(events)}"
        assert events[0] == row, "the event carries the row the store kept"
        monitor = receiver.conversation_monitors[GROUP]
        assert monitor.message_history == [], "the refused record never entered the history"
        assert receiver.p2p_manager.sent == [], "nothing was answered or relayed"


@pytest.mark.asyncio
async def test_a_raising_signature_check_is_kept_as_signature_check_failed(
        tmp_path, monkeypatch, store):
    author_node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir(exist_ok=True)
    (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(_cert_bytes(author_node_id, key))

    def raising_verify(*args, **kwargs):
        raise RuntimeError("the verifier exploded")

    monkeypatch.setattr(CommitSigner, "verify_signature", staticmethod(raising_verify))
    payload, transport = _by_way_of_a_raising_check(author_node_id, key)
    receiver = await _receive(payload, transport, author_node_id, monkeypatch, tmp_path)

    row = _one_row(store)
    assert row["reason"] == "signature_check_failed"
    assert "the verifier exploded" not in __import__("json").dumps(row), (
        "the reason is a code, never the exception's text"
    )
    assert len(_rejected_events(receiver)) == 1
    assert receiver.conversation_monitors[GROUP].message_history == []


@pytest.mark.asyncio
async def test_a_hash_mismatch_row_carries_the_recomputed_hash(tmp_path, monkeypatch, store):
    author_node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir(exist_ok=True)
    (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(_cert_bytes(author_node_id, key))
    payload, transport = _by_way_of_hash_mismatch(author_node_id, key)
    await _receive(payload, transport, author_node_id, monkeypatch, tmp_path)

    row = _one_row(store)
    expected = message_content_hash(
        conversation_id=GROUP, message_id="m-live-1", sender_node_id=author_node_id,
        sender_name="Mike (linux)", sender_type="human", agent_owner=None,
        timestamp=TIMESTAMP, content="transfer denied", tool_calls=None,
    )
    assert row["claimed_content_hash"] == payload["content_hash"]
    assert row["computed_content_hash"] == expected


@pytest.mark.asyncio
async def test_a_relayed_forgery_names_both_the_relay_and_the_claimed_author(
        tmp_path, monkeypatch, store):
    """The transport is not the author, and the row says so: they differ."""
    author_node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir(exist_ok=True)
    (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(_cert_bytes(author_node_id, key))
    payload, transport = _by_way_of_hash_mismatch(author_node_id, key)
    receiver = await _receive(payload, RELAY, author_node_id, monkeypatch, tmp_path)

    row = _one_row(store)
    assert row["transport_node_id"] == RELAY
    assert row["claimed_sender_node_id"] == author_node_id
    assert row["transport_node_id"] != row["claimed_sender_node_id"]
    assert receiver.p2p_manager.sent == []


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_forged_record_carrying_an_unpaired_surrogate_is_kept_anyway(
        tmp_path, monkeypatch, store):
    """The wire escape \\ud800 becomes a lone surrogate in the str, which utf-8
    cannot encode: the live-file write used to raise inside the store, the
    handler swallowed it, and one escape was enough to dodge the red row. The
    store sanitises every string, so the refusal is kept and shown.

    The route is the signer mismatch, which refuses before the hash is
    recomputed: a surrogate in `text` on the hash route raises inside the
    preimage encode instead, and never reaches the store at all."""
    author_node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir(exist_ok=True)
    (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(_cert_bytes(author_node_id, key))
    payload, transport = _by_way_of_signer_mismatch(author_node_id, key)
    payload["text"] = json.loads('"transfer \\ud800 denied"')
    receiver = await _receive(payload, transport, author_node_id, monkeypatch, tmp_path)

    row = _one_row(store)
    assert row["text"] == "transfer \ufffd denied"
    events = _rejected_events(receiver)
    assert len(events) == 1, "the red row is shown, not silenced by the escape"
    assert events[0] == row
    assert receiver.conversation_monitors[GROUP].message_history == []


@pytest.mark.asyncio
async def test_a_signed_record_whose_text_cannot_be_encoded_is_refused_not_crashed(
        tmp_path, monkeypatch, store):
    """A lone surrogate in `text` used to raise inside the preimage encode
    (`message_content_hash`) before any verdict was reached: `handle` crashed
    out to the router's "Error handling GROUP_TEXT" and the refusal was
    neither kept nor shown. No honestly signed record can carry such a text —
    the signer's own hash call raises on it — so it is refused as a hash
    mismatch and kept like any other forgery."""
    author_node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir(exist_ok=True)
    (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(_cert_bytes(author_node_id, key))
    payload = _signed_payload(author_node_id, CommitSigner(author_node_id, key))
    payload["text"] = json.loads('"transfer approved \\ud800 now"')
    payload["content_hash"] = "ab" * 32  # a forger cannot compute the real one
    payload["signature"] = "bm90LWEtc2lnbmF0dXJl"  # arbitrary bytes; never verified
    receiver = await _receive(payload, RELAY, author_node_id, monkeypatch, tmp_path)

    row = _one_row(store)
    assert row["reason"] == "content_hash_mismatch"
    assert row["text"] == "transfer approved \ufffd now"
    assert not any(0xD800 <= ord(cp) <= 0xDFFF for cp in row["text"])
    assert row["computed_content_hash"] is None, "there is no hash this node could recompute"
    assert row["claimed_content_hash"] == "ab" * 32
    events = _rejected_events(receiver)
    assert len(events) == 1, "the refusal by crash became a refusal kept and shown"
    assert events[0] == row
    assert receiver.conversation_monitors[GROUP].message_history == []
    assert receiver.p2p_manager.sent == [], "nothing was answered or relayed"


@pytest.mark.asyncio
async def test_a_store_that_fails_does_not_break_the_handler(tmp_path, monkeypatch, store):
    author_node_id, key = _author_key()
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    (tmp_path / "peers").mkdir(exist_ok=True)
    (tmp_path / "peers" / f"{author_node_id}.crt").write_bytes(_cert_bytes(author_node_id, key))

    def refusing_record(*args, **kwargs):
        raise RuntimeError("the store is on fire")

    monkeypatch.setattr(rejected_records.RejectedRecordStore, "record", refusing_record)
    payload, transport = _by_way_of_hash_mismatch(author_node_id, key)
    receiver = await _receive(payload, transport, author_node_id, monkeypatch, tmp_path)

    assert receiver.conversation_monitors[GROUP].message_history == []
    assert receiver.local_api.events == [], "no row was kept, so no event"
    assert not store.file_path.exists(), "nothing was stored"


def test_the_two_modules_import_without_a_cycle():
    import importlib
    import sys

    conversation_monitor = importlib.import_module("dpc_client_core.conversation_monitor")
    group_handler = importlib.import_module("dpc_client_core.message_handlers.group_handler")

    assert "dpc_client_core.rejected_records" in sys.modules
    assert conversation_monitor.rejected_records is group_handler.rejected_records
