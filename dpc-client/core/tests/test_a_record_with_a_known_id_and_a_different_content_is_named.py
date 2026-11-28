"""A record with a known id and a different content is named, not skipped.

Every dedup site dropped a record whose `id` this node already holds without
a look at its content — the in-memory set and the history file on the live
path, `add_message_with_id` on the merge path, `seen` in the repair that
folds a split folder. So a node could rewrite an old record of its own, sign
it again with its own key, and every honest node dropped the new version in
silence: the two nodes stayed different for good, and nobody was told.

Now an arriving record of a known id whose content differs from the held
copy — its text, or its `content_hash` when both copies carry one, for a
re-signed record is a new record and not an edit (ADR-042, open question 1)
— is a `conflict`: the held copy stands exactly as it is, the arriving one
is stored nowhere and relayed never, and it is kept as a rejected record
(reason `conflict`, arrived live / sync / repair by the path it came on) and
shown as the red row. The word accuses nobody: this node can see that the
two copies differ and cannot know which side moved.

The harnesses are the neighbouring ones: the merge path over a real monitor
(the refused-by-a-merge file), the live path over the real handler with a
stub receiver (the refused-live file), the repair over the split-folder
fixture, and the store's own behaviour.
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
from dpc_client_core import conversation_paths as cp
from dpc_client_core import rejected_records
from dpc_client_core.conversation_monitor import (
    ConversationMonitor,
    consolidate_conversation_stores,
)
from dpc_client_core.message_handlers.group_handler import GroupTextHandler

GROUP = "group-conflict"
BOB = "dpc-node-" + "b" * 32
SELF = "dpc-node-" + "d" * 32
RELAY = "dpc-node-86cdcd262c7f81bb58f48adbccdc86e3"
FILE = {"type": "image", "filename": "paste.png", "size_bytes": 98889, "hash": "3aea5d86"}


@pytest.fixture
def store(tmp_path):
    """The process-wide store, on the test's own folder, for this test only."""
    installed = rejected_records.RejectedRecordStore(
        tmp_path / "rejected-records" / rejected_records.FILE_NAME)
    rejected_records.set_store(installed)
    yield installed
    rejected_records.set_store(None)


PARTICIPANTS = [{"node_id": SELF, "name": "User", "context": "local"}]


def _monitor(tmp_path, cid=GROUP):
    m = ConversationMonitor(
        conversation_id=cid, participants=PARTICIPANTS, llm_manager=None, settings=None
    )
    m._get_history_path = lambda: tmp_path / cid / "history.json"
    return m


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


def _cache_the_certificate(tmp_path, monkeypatch, node_id, key):
    """Verification must fail, not say 'cannot check yet': the cert is here."""
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    peers = tmp_path / "peers"
    peers.mkdir(exist_ok=True)
    (peers / f"{node_id}.crt").write_bytes(_cert_bytes(node_id, key))


def _signed_record(signer, *, mid, content, timestamp, sender_name="Mike",
                   cid=GROUP, sender_node_id=None):
    """A record its author signed over its own preimage, as an export carries it."""
    sender = sender_node_id or signer.node_id
    record = {
        "id": mid, "role": "peer", "content": content,
        "sender_node_id": sender, "sender_name": sender_name,
        "timestamp": timestamp,
    }
    record["content_hash"] = message_content_hash(
        conversation_id=cid, message_id=mid, sender_node_id=sender,
        sender_name=sender_name, sender_type=None, agent_owner=None,
        timestamp=timestamp, content=content, tool_calls=None,
    )
    record["signature"] = signer.sign_commit(record["content_hash"])
    record["signer_node_id"] = sender
    record["preimage_version"] = PREIMAGE_VERSION
    return record


# ---------------------------------------------------------------------------
# The merge path, through the real merge_history
# ---------------------------------------------------------------------------

def test_a_known_id_with_a_different_text_is_kept_as_a_conflict(tmp_path, store):
    """The plain shape: same id, different content, the held copy untouched."""
    monitor = _monitor(tmp_path)
    held = {"id": "m-1", "role": "peer", "content": "transfer approved",
            "sender_node_id": BOB, "sender_name": "Mike",
            "timestamp": "2026-11-28T00:00:00+00:00"}
    assert monitor.add_message_with_id(dict(held)) == "added"
    before = [dict(m) for m in monitor.message_history]

    added = monitor.merge_history([dict(held, content="transfer denied")],
                                  transport_node_id=BOB)

    assert added == 0
    assert monitor.message_history == before, "the held copy stands, every field of it"
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "conflict"
    assert row["sync_count"] == 1 and row["live_count"] == 0
    assert row["transport_node_id"] == BOB
    assert row["record_id"] == "m-1"
    assert row["text"] == "transfer denied", "the row's text is the arriving copy's"
    assert monitor.last_merge_recorded == [row], "the row reaches the UI event route"
    assert monitor.last_merge_rejected == []
    assert monitor.last_merge_conflicts == 1


def test_the_cards_case_a_record_signed_again_by_its_own_author(
        tmp_path, monkeypatch, signing_identity, store):
    """THE case: the author rewrites its old record, hashes and signs the new
    content with its own key — every check passes, and it is still a conflict,
    not an update. Nothing but the name answers the forgery this good."""
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity.node_id,
                           signing_identity.private_key)
    monitor = _monitor(tmp_path)
    first = _signed_record(signing_identity, mid="m-1", content="transfer approved",
                           timestamp="2026-11-28T00:00:00+00:00")
    assert monitor.merge_history([first], transport_node_id=BOB) == 1
    before = [dict(m) for m in monitor.message_history]

    rewritten = _signed_record(signing_identity, mid="m-1", content="transfer denied",
                               timestamp="2026-11-28T00:00:00+00:00")
    added = monitor.merge_history([rewritten], transport_node_id=BOB)

    assert added == 0
    assert monitor.message_history == before, "the first copy stands, field for field"
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "conflict"
    assert rows[0]["text"] == "transfer denied"
    assert rows[0]["claimed_sender_node_id"] == signing_identity.node_id
    assert monitor.last_merge_conflicts == 1


def test_the_same_id_content_and_hash_again_is_a_plain_duplicate(
        tmp_path, monkeypatch, signing_identity, store):
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity.node_id,
                           signing_identity.private_key)
    monitor = _monitor(tmp_path)
    first = _signed_record(signing_identity, mid="m-1", content="transfer approved",
                           timestamp="2026-11-28T00:00:00+00:00")
    assert monitor.merge_history([first], transport_node_id=BOB) == 1
    before = [dict(m) for m in monitor.message_history]

    assert monitor.add_message_with_id(dict(first)) == "duplicate"
    added = monitor.merge_history([dict(first)], transport_node_id=BOB)

    assert added == 0
    assert monitor.message_history == before
    assert store.for_conversation(GROUP) == []
    assert monitor.last_merge_conflicts == 0


def test_the_same_text_under_a_different_hash_is_a_conflict(
        tmp_path, monkeypatch, signing_identity, store):
    """Two signed copies of one id with the same words and different hashes: the
    preimage moved (here the timestamp), and a re-signed record is a new record,
    not an edit (ADR-042, open question 1)."""
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity.node_id,
                           signing_identity.private_key)
    monitor = _monitor(tmp_path)
    first = _signed_record(signing_identity, mid="m-1", content="transfer approved",
                           timestamp="2026-11-28T00:00:00+00:00")
    assert monitor.merge_history([first], transport_node_id=BOB) == 1

    rehashed = _signed_record(signing_identity, mid="m-1", content="transfer approved",
                              timestamp="2026-11-28T01:00:00+00:00")
    added = monitor.merge_history([rehashed], transport_node_id=BOB)

    assert added == 0
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "conflict"
    assert rows[0]["text"] == "transfer approved", "the words are the same; the hash is not"
    assert rows[0]["claimed_content_hash"] == rehashed["content_hash"]
    assert monitor.last_merge_conflicts == 1


def test_the_same_record_signed_again_is_a_plain_duplicate(
        tmp_path, monkeypatch, signing_identity, store):
    """The hash is the preimage's, not the signature's: `message_content_hash`
    covers the record's fields and never the signature or the key. A record
    signed a second time over the same fields — the same key, a fresh
    signature — is the same record and no conflict, so a re-issued certificate
    or a re-signed history gives no red list. A different key is a different
    node id: that signature does not verify, and the record is refused by the
    signature before any dedup looks at it (the test two above)."""
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity.node_id,
                           signing_identity.private_key)
    monitor = _monitor(tmp_path)
    first = _signed_record(signing_identity, mid="m-1", content="transfer approved",
                           timestamp="2026-11-28T00:00:00+00:00")
    assert monitor.merge_history([first], transport_node_id=BOB) == 1

    again = _signed_record(signing_identity, mid="m-1", content="transfer approved",
                           timestamp="2026-11-28T00:00:00+00:00")
    assert again["content_hash"] == first["content_hash"], "the hash did not move with the signature"
    assert monitor.merge_history([again], transport_node_id=BOB) == 0

    assert store.for_conversation(GROUP) == [], "a re-signature is not a second version"
    assert monitor.last_merge_conflicts == 0
    assert monitor.last_merge_rejected == []


def test_a_legacy_held_copy_without_a_hash_is_not_confused_with_a_signed_one(
        tmp_path, monkeypatch, signing_identity, store):
    """The hash half fires only when both copies carry one: a legacy record
    held with no hash and a signed arrival of the same words is a duplicate."""
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity.node_id,
                           signing_identity.private_key)
    monitor = _monitor(tmp_path)
    legacy = {"id": "m-1", "role": "peer", "content": "the plan stands",
              "sender_node_id": signing_identity.node_id, "sender_name": "Mike",
              "timestamp": "2026-11-28T00:00:00+00:00"}
    assert monitor.merge_history([legacy], transport_node_id=BOB) == 1
    before = [dict(m) for m in monitor.message_history]

    signed_same_words = _signed_record(signing_identity, mid="m-1", content="the plan stands",
                                       timestamp="2026-11-28T00:00:00+00:00")
    added = monitor.merge_history([signed_same_words], transport_node_id=BOB)

    assert added == 0
    assert monitor.message_history == before
    assert store.for_conversation(GROUP) == []
    assert monitor.last_merge_conflicts == 0


def test_a_bad_signature_of_a_known_id_is_refused_by_the_signature_not_named_a_conflict(
        tmp_path, monkeypatch, signing_identity, store):
    """The signature check still comes first: a forged record of a known id is
    `rejected` with the signature's own reason, never `conflict`."""
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity.node_id,
                           signing_identity.private_key)
    monitor = _monitor(tmp_path)
    held = {"id": "m-1", "role": "peer", "content": "transfer approved",
            "sender_node_id": signing_identity.node_id, "sender_name": "Mike",
            "timestamp": "2026-11-28T00:00:00+00:00"}
    assert monitor.add_message_with_id(dict(held)) == "added"

    forged = _signed_record(signing_identity, mid="m-1", content="transfer denied",
                            timestamp="2026-11-28T00:00:00+00:00")
    stranger_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged["signature"] = CommitSigner(
        "dpc-node-" + "c" * 32, stranger_key).sign_commit(forged["content_hash"])
    added = monitor.merge_history([forged], transport_node_id=BOB)

    assert added == 0
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "invalid_signature", "the signature's word, not the conflict's"
    assert len(monitor.last_merge_rejected) == 1
    assert monitor.last_merge_conflicts == 0


def test_the_replacement_of_a_local_file_note_is_no_conflict(tmp_path, store):
    """This node's own «Received file» note and the sender's record of that
    file can share an id and differ in words: the drop of the note comes first
    (`_drop_local_file_note`), and one file keeps one record — no red row."""
    monitor = _monitor(tmp_path)
    note = {"id": "file-1", "role": "assistant",
            "content": "Received screenshot: paste.png (0.09 MB)",
            "sender_node_id": BOB, "signer_node_id": SELF,
            "attachments": [FILE], "timestamp": "2026-11-28T00:00:00+00:00"}
    monitor.message_history.append(dict(note))
    monitor.message_ids.add("file-1")
    monitor._local_file_notes.add("file-1")
    monitor.restore_chronological_order = lambda: None

    theirs = {"id": "file-1", "role": "user",
              "content": "Sent screenshot: paste.png (0.09 MB)",
              "sender_node_id": BOB, "signer_node_id": BOB,
              "attachments": [FILE], "timestamp": "2026-11-28T00:01:00+00:00"}
    added = monitor.merge_history([theirs], transport_node_id=BOB)

    assert added == 1
    assert store.for_conversation(GROUP) == [], "a replacement is not an accusation"
    assert monitor.last_merge_conflicts == 0
    assert [m["id"] for m in monitor.message_history] == ["file-1"]
    assert monitor.message_history[0]["content"] == "Sent screenshot: paste.png (0.09 MB)"


def test_add_message_with_id_answers_a_word_and_adds_nothing_but_the_new():
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.message_history = []
    monitor.message_ids = set()
    monitor._history_dirty = False
    monitor._local_node_id = lambda: SELF

    assert monitor.add_message_with_id(
        {"id": "n-1", "role": "peer", "content": "one"}) == "added"
    assert monitor.add_message_with_id(
        {"id": "n-1", "role": "peer", "content": "one"}) == "duplicate"
    assert monitor.add_message_with_id(
        {"id": "n-1", "role": "peer", "content": "two"}) == "conflict"

    assert len(monitor.message_history) == 1, "neither a duplicate nor a conflict is stored"
    assert monitor.message_history[0]["content"] == "one"
    assert monitor.message_ids == {"n-1"}


# ---------------------------------------------------------------------------
# The live path, through the real GroupTextHandler.handle
# ---------------------------------------------------------------------------

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

    def __init__(self, node_id, members, monkeypatch):
        self.node_id = node_id
        self.p2p_manager = _P2P(node_id)
        self.group_manager = self
        self.members = list(members)
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


def _signed_payload(author_node_id, signer, text, message_id="m-live-1",
                    timestamp="2026-11-28T00:00:00+00:00"):
    """A GROUP_TEXT as the author's node would send it."""
    payload = {
        "group_id": GROUP, "text": text, "sender_name": "Mike (linux)",
        "sender_type": "human", "sender_node_id": author_node_id, "agent_owner": None,
        "message_id": message_id, "timestamp": timestamp,
        "mentions": [],
    }
    payload["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=message_id,
        sender_node_id=author_node_id, sender_name="Mike (linux)", sender_type="human",
        agent_owner=None, timestamp=timestamp, content=text,
        tool_calls=None,
    )
    payload["signature"] = signer.sign_commit(payload["content_hash"])
    payload["signer_node_id"] = author_node_id
    payload["preimage_version"] = PREIMAGE_VERSION
    return payload


async def _receive_both(receiver, first, second, transport_of_second):
    """Two GROUP_TEXT frames through the real handler, one receiver, one session."""
    author = first["sender_node_id"]
    await GroupTextHandler(service=receiver).handle(author, first)
    await GroupTextHandler(service=receiver).handle(transport_of_second, second)
    return receiver


def _rejected_events(receiver):
    return [payload for name, payload in receiver.local_api.events
            if name == rejected_records.UI_EVENT]


@pytest.mark.asyncio
async def test_a_live_record_of_a_known_id_with_different_text_is_named(
        tmp_path, monkeypatch, store):
    """Red on the old code: the in-memory dedup dropped the second frame before
    anything looked at its content."""
    author_node_id, key = _author_key()
    _cache_the_certificate(tmp_path, monkeypatch, author_node_id, key)
    receiver = _Receiver(SELF, [SELF, author_node_id], monkeypatch)
    author = CommitSigner(author_node_id, key)
    a = _signed_payload(author_node_id, author, "transfer approved")
    b = _signed_payload(author_node_id, author, "transfer denied")

    await _receive_both(receiver, a, b, author_node_id)

    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "conflict"
    assert row["live_count"] == 1 and row["sync_count"] == 0
    assert row["record_id"] == "m-live-1"
    assert row["text"] == "transfer denied"
    events = _rejected_events(receiver)
    assert len(events) == 1, "one event, and nothing else to the chat about it"
    assert events[0] == row
    monitor = receiver.conversation_monitors[GROUP]
    assert [m["id"] for m in monitor.message_history] == ["m-live-1"]
    assert monitor.message_history[0]["content"] == "transfer approved"
    assert receiver.p2p_manager.sent == [], "nothing was answered or relayed"


@pytest.mark.asyncio
async def test_a_relayed_rewrite_names_the_relay_and_the_claimed_author(
        tmp_path, monkeypatch, store):
    author_node_id, key = _author_key()
    _cache_the_certificate(tmp_path, monkeypatch, author_node_id, key)
    receiver = _Receiver(SELF, [SELF, author_node_id, RELAY], monkeypatch)
    author = CommitSigner(author_node_id, key)
    a = _signed_payload(author_node_id, author, "transfer approved")
    b = _signed_payload(author_node_id, author, "transfer denied")

    await _receive_both(receiver, a, b, RELAY)

    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "conflict"
    assert rows[0]["transport_node_id"] == RELAY, "the row names who handed it over"
    assert rows[0]["claimed_sender_node_id"] == author_node_id
    assert receiver.conversation_monitors[GROUP].message_history[0][
        "content"] == "transfer approved"
    assert receiver.p2p_manager.sent == []


@pytest.mark.asyncio
async def test_a_live_record_of_a_known_id_with_the_same_text_is_still_a_duplicate(
        tmp_path, monkeypatch, store):
    author_node_id, key = _author_key()
    _cache_the_certificate(tmp_path, monkeypatch, author_node_id, key)
    receiver = _Receiver(SELF, [SELF, author_node_id], monkeypatch)
    author = CommitSigner(author_node_id, key)
    a = _signed_payload(author_node_id, author, "transfer approved")
    b = _signed_payload(author_node_id, author, "transfer approved")

    await _receive_both(receiver, a, b, author_node_id)

    assert store.for_conversation(GROUP) == [], "no row for a plain duplicate"
    assert _rejected_events(receiver) == [], "and no event"
    assert [m["id"] for m in
            receiver.conversation_monitors[GROUP].message_history] == ["m-live-1"]


@pytest.mark.asyncio
async def test_the_senders_record_of_a_file_this_node_already_noted_is_no_conflict(
        tmp_path, monkeypatch, store):
    """This node's «Received file» note carries the author's message id and other
    words. The merge drops the note before it looks for a conflict
    (`_drop_local_file_note`, by the file); a live frame carries no file to
    match by, so a held note answers "not a conflict" and the frame falls to the
    dedup, as it did before there was a conflict. A red row here would accuse
    every file whose transfer reached this node before its sender's record."""
    author_node_id, key = _author_key()
    _cache_the_certificate(tmp_path, monkeypatch, author_node_id, key)
    receiver = _Receiver(SELF, [SELF, author_node_id], monkeypatch)
    monitor = receiver._get_or_create_conversation_monitor(GROUP)
    # The note as file_transfer_manager writes it when a transfer completes.
    monitor.add_message(
        "assistant", "Received file: paste.png", [dict(FILE)],
        timestamp="2026-11-28T00:00:00+00:00",
        sender_node_id=author_node_id, sender_name="Mike (linux)",
        message_id="m-live-1",
    )
    monitor.note_local_file_record("m-live-1")
    author = CommitSigner(author_node_id, key)
    theirs = _signed_payload(author_node_id, author, "here is the screenshot")

    await GroupTextHandler(service=receiver).handle(author_node_id, theirs)

    assert store.for_conversation(GROUP) == [], "the record of a file is not an accusation"
    assert _rejected_events(receiver) == []
    assert [m["content"] for m in monitor.message_history] == ["Received file: paste.png"], \
        "the frame fell to the dedup, as before: the note stands"
    assert receiver.p2p_manager.sent == []


# ---------------------------------------------------------------------------
# The repair that folds a split folder
# ---------------------------------------------------------------------------

def _store_folder(base, name, messages):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "history.json").write_text(
        json.dumps({"conversation_id": GROUP, "version": 1, "messages": messages}),
        encoding="utf-8",
    )
    return d


def test_a_split_folders_record_of_a_known_id_with_different_content_is_kept(
        tmp_path, store):
    """The repair named the differing orphan copy nowhere: it silently kept the
    canonical one. Now the orphan copy is a rejected record arrived `repair`
    (no node handed it over — it was on this disk all along), and nothing is
    deleted: the retired folder keeps its own copy."""
    _store_folder(tmp_path, GROUP, [
        {"id": "m-1", "role": "user", "sender_name": "Ark",
         "content": "transfer approved", "timestamp": "2026-08-04T11:21:51+00:00"},
    ])
    _store_folder(tmp_path, f"{GROUP}-work", [
        {"id": "m-1", "role": "user", "sender_name": "Ark",
         "content": "transfer denied", "timestamp": "2026-08-04T11:21:51+00:00"},
    ])

    summary = consolidate_conversation_stores(tmp_path, GROUP, "work")

    assert summary["conflicts"] == 1
    canonical = cp.resolve_store_dir(tmp_path, GROUP, "work")
    merged = json.loads(
        (canonical / "history.json").read_text(encoding="utf-8"))["messages"]
    assert [m["id"] for m in merged] == ["m-1"]
    assert merged[0]["content"] == "transfer approved", "the canonical copy stands"
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "conflict"
    assert row["repair_count"] == 1 and row["live_count"] == 0 and row["sync_count"] == 0
    assert row["transport_node_id"] is None
    assert row["text"] == "transfer denied", "the row's text is the arriving copy's"
    retired = [d for d in tmp_path.iterdir() if cp.RETIRED_MARKER in d.name]
    assert len(retired) == 1, "the emptied folder is renamed, not removed"
    kept = json.loads(
        (retired[0] / "history.json").read_text(encoding="utf-8"))["messages"]
    assert kept[0]["content"] == "transfer denied", "nothing is lost on disk"


# ---------------------------------------------------------------------------
# The store's own words
# ---------------------------------------------------------------------------

def test_the_store_keeps_a_conflict_that_arrived_by_repair(tmp_path):
    store = rejected_records.RejectedRecordStore(
        tmp_path / "state" / rejected_records.FILE_NAME)

    entry, is_new = store.record(
        conversation_id=GROUP, reason="conflict", arrived="repair",
        record_id="m-1", text="transfer denied",
    )

    assert is_new is True
    assert entry["reason"] == "conflict"
    assert entry["repair_count"] == 1
    assert entry["live_count"] == 0 and entry["sync_count"] == 0
    keys = list(entry)
    assert keys.index("repair_count") == keys.index("sync_count") + 1, (
        "repair_count sits right after sync_count in the row's key order"
    )
