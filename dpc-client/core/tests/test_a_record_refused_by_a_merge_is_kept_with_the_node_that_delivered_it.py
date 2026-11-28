"""A record refused by a merge is kept, with the node that delivered it.

The merge path (`ConversationMonitor.merge_history`) refused a bad record by
skipping it: `last_merge_rejected` said which ids would never come, and
nothing else did. Now each refusal is also kept in the rejected-records store
(arrived `sync`, the delivering node named by the caller) and its row is left
in `last_merge_recorded`, so `GroupHistoryResponseHandler` can tell the open
chat one `group_record_rejected` event per red row — the same event the live
path sends.

The monitor here is the real one over a tmp history file, the seam the
neighbouring merge tests use; the shape of `last_merge_rejected` itself is
already covered there and only asserted not to have moved.
"""

import datetime
import json
import types

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from dpc_protocol import commit_integrity
from dpc_protocol.commit_integrity import CommitSigner
from dpc_protocol.message_signing import PREIMAGE_VERSION, message_content_hash
from dpc_client_core import rejected_records
from dpc_client_core.conversation_monitor import ConversationMonitor

GROUP = "group-merge"
BOB = "dpc-node-" + "b" * 32
SELF = "dpc-node-" + "d" * 32

REJECTED_KEYS = {"id", "content_hash", "sender_node_id", "sender_name", "verdict"}


@pytest.fixture
def store(tmp_path):
    """The process-wide store, on the test's own folder, for this test only."""
    installed = rejected_records.RejectedRecordStore(
        tmp_path / "rejected-records" / rejected_records.FILE_NAME)
    rejected_records.set_store(installed)
    yield installed
    rejected_records.set_store(None)


PARTICIPANTS = [{"node_id": SELF, "name": "User", "context": "local"}]


def _monitor(tmp_path, cid=GROUP, settings=None):
    m = ConversationMonitor(
        conversation_id=cid, participants=PARTICIPANTS, llm_manager=None, settings=settings
    )
    m._get_history_path = lambda: tmp_path / cid / "history.json"
    return m


class _Settings:
    def __init__(self, reject):
        self._reject = reject

    def get_reject_unsigned_history(self) -> bool:
        return self._reject


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


def _cache_the_certificate(tmp_path, monkeypatch, signing_identity):
    """Verification must fail, not say 'cannot check yet': the cert is here."""
    monkeypatch.setattr(commit_integrity, "DPC_HOME_DIR", tmp_path)
    peers = tmp_path / "peers"
    peers.mkdir(exist_ok=True)
    (peers / f"{signing_identity.node_id}.crt").write_bytes(
        _cert_bytes(signing_identity.node_id, signing_identity.private_key))


def _forged(signing_identity, *, mid="bad-1", content="forged text"):
    """A merge record whose hash is right and whose signature is somebody
    else's: a genuine bad signature, not an uncacheable signer."""
    sender = signing_identity.node_id
    record = {
        "id": mid, "role": "peer", "content": content,
        "sender_node_id": sender, "sender_name": "Mike",
        "timestamp": "2026-11-28T00:00:00+00:00",
    }
    record["content_hash"] = message_content_hash(
        conversation_id=GROUP, message_id=mid, sender_node_id=sender,
        sender_name="Mike", sender_type=None, agent_owner=None,
        timestamp=record["timestamp"], content=content, tool_calls=None,
    )
    stranger_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    stranger = CommitSigner("dpc-node-" + "c" * 32, stranger_key)
    record["signature"] = stranger.sign_commit(record["content_hash"])
    record["signer_node_id"] = sender
    record["preimage_version"] = PREIMAGE_VERSION
    return record


def _signed_export(tmp_path, signing_identity, cid=GROUP):
    sender = _monitor(tmp_path, cid)
    for text in ("first", "second"):
        sender.add_message(
            role="user", content=text,
            sender_node_id=signing_identity.node_id, sender_name="Mike",
        )
    return sender.export_history()


def test_a_bad_signature_is_kept_with_the_node_that_delivered_it(
        tmp_path, monkeypatch, signing_identity, store):
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity)
    monitor = _monitor(tmp_path)

    added = monitor.merge_history([_forged(signing_identity)], transport_node_id=BOB)

    assert added == 0
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    row = rows[0]
    assert row["reason"] == "invalid_signature"
    assert row["sync_count"] == 1
    assert row["live_count"] == 0
    assert row["transport_node_id"] == BOB, "the row names who handed the batch over"
    assert row["conversation_id"] == GROUP
    assert row["record_id"] == "bad-1"
    assert row["claimed_sender_node_id"] == signing_identity.node_id
    assert row["text"] == "forged text"
    # The refusal list readers already know keeps its shape and its verdict.
    assert len(monitor.last_merge_rejected) == 1
    assert set(monitor.last_merge_rejected[0]) == REJECTED_KEYS
    assert monitor.last_merge_rejected[0]["verdict"].startswith("invalid signature from")
    assert monitor.message_history == []


def test_an_unsigned_record_is_kept_when_the_setting_refuses_it(
        tmp_path, monkeypatch, signing_identity, store):
    monitor = _monitor(tmp_path, settings=_Settings(True))

    added = monitor.merge_history(
        [{"id": "x", "role": "peer", "content": "no signature"}],
        transport_node_id=BOB,
    )

    assert added == 0
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "unsigned"
    assert rows[0]["sync_count"] == 1
    assert rows[0]["transport_node_id"] == BOB


def test_a_record_over_an_unknown_preimage_is_kept_as_unknown_preimage(
        tmp_path, monkeypatch, signing_identity, store):
    exported = _signed_export(tmp_path, signing_identity)
    for msg in exported:
        msg["preimage_version"] = "dptp-msg-v99"
    monitor = _monitor(tmp_path, settings=_Settings(True))

    added = monitor.merge_history(exported, transport_node_id=BOB)

    assert added == 0
    rows = store.for_conversation(GROUP)
    assert len(rows) == 2
    assert {row["reason"] for row in rows} == {"unknown_preimage"}
    assert all(row["sync_count"] == 1 for row in rows)


def test_with_the_setting_off_nothing_is_kept_the_record_is_legacy(
        tmp_path, monkeypatch, signing_identity, store):
    monitor = _monitor(tmp_path)  # no settings: reject_unsigned off

    added = monitor.merge_history(
        [{"id": "x", "role": "peer", "content": "no signature"}],
        transport_node_id=BOB,
    )

    assert added == 1
    assert monitor.message_history[-1]["verification"] == "legacy"
    assert store.for_conversation(GROUP) == []
    assert monitor.last_merge_recorded == []


def _poisoned(signing_identity, *, mid="bad-poison"):
    """A signed record whose text carries a lone surrogate. The recompute
    cannot even encode that text — and no honest signer could have signed over
    it, for its own `message_content_hash` raises on the same text — so the
    `content_hash` and `signature` are a forger's arbitrary bytes."""
    return {
        "id": mid, "role": "peer", "content": json.loads('"transfer approved \\ud800 now"'),
        "sender_node_id": signing_identity.node_id, "sender_name": "Mike",
        "timestamp": "2026-11-28T00:00:00+00:00",
        "content_hash": "ab" * 32,
        "signature": "bm90LWEtc2lnbmF0dXJl",
        "signer_node_id": signing_identity.node_id,
        "preimage_version": PREIMAGE_VERSION,
    }


def test_a_poisoned_record_is_refused_alone_and_the_rest_of_the_batch_merges(
        tmp_path, signing_identity, store):
    """A signed record whose text cannot be encoded used to raise inside
    `_recompute_hash` and abort the whole merge: every record after the
    poisoned one in the batch was left unmerged, on every sync, and the
    refusal was kept nowhere. Now the poisoned record is refused on its own —
    as a hash mismatch, kept and counted — and the batch goes on around it."""
    monitor = _monitor(tmp_path)
    good_first = {"id": "good-1", "role": "peer", "content": "fine before"}
    good_last = {"id": "good-2", "role": "peer", "content": "fine after"}

    added = monitor.merge_history([good_first, _poisoned(signing_identity), good_last],
                                  transport_node_id=BOB)

    assert added == 2, "both good records merged around the poisoned one"
    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "content_hash_mismatch"
    assert rows[0]["record_id"] == "bad-poison"
    assert rows[0]["text"] == "transfer approved \ufffd now"
    assert not any(0xD800 <= ord(cp) <= 0xDFFF for cp in rows[0]["text"])
    assert rows[0]["transport_node_id"] == BOB
    assert len(monitor.last_merge_rejected) == 1
    assert monitor.last_merge_rejected[0]["id"] == "bad-poison"
    assert sorted(record["id"] for record in monitor.message_history) == ["good-1", "good-2"]


def test_last_merge_recorded_holds_exactly_this_merges_rows_and_is_reset(
        tmp_path, monkeypatch, signing_identity, store):
    _cache_the_certificate(tmp_path, monkeypatch, signing_identity)
    monitor = _monitor(tmp_path)

    monitor.merge_history(
        [_forged(signing_identity, mid="bad-1"), _forged(signing_identity, mid="bad-2")],
        transport_node_id=BOB,
    )
    assert [row["record_id"] for row in monitor.last_merge_recorded] == ["bad-1", "bad-2"]
    assert all(row["reason"] == "invalid_signature" for row in monitor.last_merge_recorded)
    assert len(monitor.last_merge_rejected) == 2

    monitor.merge_history([], transport_node_id=BOB)
    assert monitor.last_merge_recorded == [], "the next merge starts its own list"
    assert monitor.last_merge_rejected == []


@pytest.mark.asyncio
async def test_through_the_history_response_handler_one_event_per_kept_row(
        tmp_path, monkeypatch, signing_identity, store):
    from dpc_client_core.message_handlers.chat_history_handlers import HistoryRequestRegistry
    from dpc_client_core.message_handlers.group_handler import GroupHistoryResponseHandler

    _cache_the_certificate(tmp_path, monkeypatch, signing_identity)
    monitor = _monitor(tmp_path)

    class _Api:
        def __init__(self):
            self.events = []

        async def broadcast_event(self, name, payload):
            self.events.append((name, payload))

    service = types.SimpleNamespace(
        group_manager=types.SimpleNamespace(
            get_group=lambda gid: types.SimpleNamespace(
                group_id=GROUP, members=[SELF, BOB, signing_identity.node_id]),
        ),
        p2p_manager=types.SimpleNamespace(node_id=SELF, peers={}),
        history_requests=HistoryRequestRegistry(),
        conversation_monitors={GROUP: monitor},
        local_api=_Api(),
        knowledge_service=None,
    )
    service._get_or_create_conversation_monitor = lambda gid: monitor
    service.history_requests.note(BOB, GROUP, "r-1")

    records = [_forged(signing_identity, mid="bad-1"),
               _forged(signing_identity, mid="bad-2")]
    await GroupHistoryResponseHandler(service).handle(
        BOB, {"group_id": GROUP, "history": records, "request_id": "r-1"},
    )

    red = [payload for name, payload in service.local_api.events
           if name == rejected_records.UI_EVENT]
    assert len(red) == 2, "one event per kept row, and nothing else to the chat about them"
    assert {row["record_id"] for row in red} == {"bad-1", "bad-2"}
    assert all(row["transport_node_id"] == BOB for row in red), (
        "the event names the node that delivered the batch"
    )
    assert monitor.message_history == []


@pytest.mark.asyncio
async def test_a_refusal_on_the_chat_history_route_names_its_deliverer_too(
        tmp_path, monkeypatch, signing_identity, store):
    """The claimed-request route (`ChatHistoryResponseHandler`) also merges a
    GROUP history, and it used to call merge_history with no delivering node:
    a refusal kept there named nobody, and «who handed it over» is the lead the
    store exists for. The sender is a member and is right there in the frame."""
    from dpc_client_core.message_handlers.chat_history_handlers import (
        ChatHistoryResponseHandler,
        HistoryRequestRegistry,
    )

    _cache_the_certificate(tmp_path, monkeypatch, signing_identity)
    monitor = _monitor(tmp_path)

    class _Api:
        def __init__(self):
            self.events = []

        async def broadcast_event(self, name, payload):
            self.events.append((name, payload))

    api = _Api()
    service = types.SimpleNamespace(
        conversation_monitors={GROUP: monitor},
        group_manager=types.SimpleNamespace(
            get_group=lambda gid: types.SimpleNamespace(members=[BOB, SELF])
            if gid == GROUP else None,
        ),
        local_api=api,
        history_requests=HistoryRequestRegistry(),
    )
    service.history_requests.note(BOB, GROUP, "r-chat")

    await ChatHistoryResponseHandler(service).handle(BOB, {
        "conversation_id": GROUP,
        "request_id": "r-chat",
        "messages": [_forged(signing_identity, mid="bad-chat")],
    })

    rows = store.for_conversation(GROUP)
    assert len(rows) == 1
    assert rows[0]["reason"] == "invalid_signature"
    assert rows[0]["transport_node_id"] == BOB, "the member that handed the batch over"
    assert rows[0]["sync_count"] == 1
    red = [payload for name, payload in api.events if name == rejected_records.UI_EVENT]
    assert len(red) == 1, "one red-row event, as the group route sends"
    assert red[0] == rows[0]
    assert any(name == "history_restored" for name, _ in api.events), (
        "the route's own broadcast is unchanged"
    )
    assert monitor.message_history == []
