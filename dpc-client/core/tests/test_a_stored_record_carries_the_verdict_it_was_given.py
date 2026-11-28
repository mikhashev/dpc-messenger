"""A record's verdict is stored on it, and outside both of its hashes.

A record that fails or skips the signature check showed nothing to the person
or the agent, because nothing was stored: the live path computed a verdict,
broadcast it to the UI and dropped it (the older card
THE-VERDICT-OF-A-LIVE-RECORD-IS-BROADCAST-AND-NEVER-STORED), and a record this
node wrote carried no mark at all. `add_message` now writes a `verification`
key: "own" for this node's records (Mike's word, DPC #111/#112/#126: "on own -
yes"), the verdict the caller checked for a peer's, and never a guess.

The key sits after `chain_hash` and `content_hash`, not inside either: the
chain covers six named fields and the signature preimage named arguments, so a
new key changes neither. Both halves are pinned, and they need different tests.
The path (through the real `add_message`) proves the key is set after the
hashes - and only that: with the key set last, the path stays green whatever
the formula reads, because the key is not in the record yet when the hashes
run. The formula itself is pinned on `chain_hash_for` directly, on a record
with the key already in it - the one test that fails the day someone widens
the formula to the whole record.
"""

import json

import pytest

from dpc_protocol.message_signing import message_content_hash
from dpc_client_core.conversation_monitor import ConversationMonitor, chain_hash_for


GROUP = "group-1234"
AUTHOR = "dpc-node-6d218e95dee9cfeebfc3caa705ae8c95"
TS = "2026-11-28T00:00:00+00:00"
PARTICIPANTS = [{"node_id": "n1", "name": "User", "context": "local"}]


@pytest.fixture(autouse=True)
def _no_machine_key(monkeypatch):
    """Sign with nothing rather than with ~/.dpc/node.key: no test here reads a
    signature, and the machine's own key must stay untouched."""
    monkeypatch.setattr(ConversationMonitor, "_get_signer", lambda self: None)


@pytest.fixture(autouse=True)
def _always_persist(monkeypatch):
    """persist_history is a property backed by an on-disk settings file."""
    monkeypatch.setattr(ConversationMonitor, "persist_history", property(lambda self: True))


def _monitor(root, cid=GROUP):
    m = ConversationMonitor(conversation_id=cid, participants=PARTICIPANTS, llm_manager=None)
    m._get_history_path = lambda: root / cid / "history.json"
    return m


def _fields(content="transfer approved"):
    """What an author would put on the wire beside the message."""
    return {
        "content_hash": message_content_hash(
            conversation_id=GROUP,
            message_id="m-1",
            sender_node_id=AUTHOR,
            sender_name="Mike (linux)",
            sender_type="human",
            agent_owner=None,
            timestamp=TS,
            content=content,
            tool_calls=None,
        ),
        "signature": "not-checked-here",
        "signer_node_id": AUTHOR,
    }


def _stored(monitor, **kwargs):
    """One record through the real add_message, and the dict it left."""
    args = dict(
        role="peer", content="transfer approved", timestamp=TS,
        sender_node_id=AUTHOR, sender_name="Mike (linux)", message_id="m-1",
        sender_type="human",
    )
    args.update(kwargs)
    monitor.add_message(**args)
    return monitor.message_history[-1]


def test_an_own_record_is_stored_as_own_and_survives_a_reload(tmp_path):
    monitor = _monitor(tmp_path)
    stored = _stored(monitor, role="user", sender_node_id="n1",
                     signature_fields=None, verification=None)
    assert stored["verification"] == "own"
    assert monitor.save_history() is True

    reloaded = _monitor(tmp_path)
    assert reloaded.load_history() is True
    assert reloaded.message_history[-1]["verification"] == "own"


def test_a_peer_record_stores_the_verdict_the_caller_checked_and_never_own(tmp_path):
    for verdict in ("verified", "unverified"):
        stored = _stored(_monitor(tmp_path / verdict),
                         signature_fields=_fields(), verification=verdict)
        assert stored["verification"] == verdict
        assert stored["verification"] != "own"


def test_a_peer_record_with_no_verdict_passed_gets_no_key(tmp_path):
    """Not "own", not a guess: the caller checked and said nothing."""
    stored = _stored(_monitor(tmp_path), signature_fields=_fields())
    assert "verification" not in stored


def test_an_explicit_verdict_wins_on_an_own_record(tmp_path):
    """An import or a test may say "legacy" about a record with no fields."""
    stored = _stored(_monitor(tmp_path), verification="legacy")
    assert stored["verification"] == "legacy"


def test_the_verdict_word_sits_outside_both_hashes_of_a_peer_record(tmp_path):
    left = _monitor(tmp_path / "left")
    right = _monitor(tmp_path / "right")
    own = _stored(left, signature_fields=None)                       # -> "own"
    flagged = _stored(right, signature_fields=_fields(),
                      verification="unverified")

    # Not vacuous: the two records carry different verdict words over
    # otherwise identical bytes, so a hash that read the word would differ.
    assert own["verification"] == "own"
    assert flagged["verification"] == "unverified"
    assert own["chain_hash"] == flagged["chain_hash"]
    assert own["content_hash"] == flagged["content_hash"]

    # And each is the formula's own value over the record as stored — the
    # chain over its six named fields from genesis, the signature over its
    # named arguments — with `verification` present in the dict it read.
    assert own["chain_hash"] == chain_hash_for(own, "genesis")
    assert own["content_hash"] == message_content_hash(
        conversation_id=GROUP, message_id="m-1", sender_node_id=AUTHOR,
        sender_name="Mike (linux)", sender_type="human", agent_owner=None,
        timestamp=TS, content="transfer approved", tool_calls=None,
    )


def test_the_verdict_word_sits_outside_both_hashes_of_an_own_record(tmp_path):
    """The minted hash too: add_message makes content_hash for this node's
    records, and the word must not reach the minting either."""
    left = _monitor(tmp_path / "left")
    right = _monitor(tmp_path / "right")
    plain = _stored(left, role="user", sender_node_id="n1")
    marked = _stored(right, role="user", sender_node_id="n1", verification="legacy")

    assert plain["verification"] == "own"
    assert marked["verification"] == "legacy"
    assert plain["chain_hash"] == marked["chain_hash"]
    assert plain["content_hash"] == marked["content_hash"]
    assert plain["content_hash"] == message_content_hash(
        conversation_id=GROUP, message_id="m-1", sender_node_id="n1",
        sender_name="Mike (linux)", sender_type="human", agent_owner=None,
        timestamp=TS, content="transfer approved", tool_calls=None,
    )


def test_the_chain_formula_does_not_read_a_verdict_key_already_in_the_record():
    """The formula, not the path. The two tests above put the key in after the
    hashes run, so they cannot tell what `chain_hash_for` reads: they stayed
    green when it was changed to hash the whole record. Here the key is in the
    dict BEFORE the formula runs, as it would be the day someone moves the
    assignment up - and then only the formula's own choice of fields keeps the
    chain stable."""
    record = {"id": "m-1", "role": "user", "content": "transfer approved",
              "sender_name": "Mike (linux)", "timestamp": TS, "msg_index": 1}
    plain = chain_hash_for(dict(record), "genesis")
    for word in ("own", "unverified", "legacy", "verified", "a word not in the dictionary"):
        marked = dict(record, verification=word)
        assert chain_hash_for(marked, "genesis") == plain, f"the chain moved on {word!r}"
    # Not vacuous: a field the formula does read still moves it.
    assert chain_hash_for(dict(record, content="something else"), "genesis") != plain


def test_an_old_record_with_no_verdict_still_loads_and_reads_legacy(tmp_path):
    """Everything already on disk: no key, and the readers' default stands."""
    monitor = _monitor(tmp_path)
    _stored(monitor, role="user", sender_node_id="n1")
    monitor.save_history()
    path = tmp_path / GROUP / "history.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    del on_disk["messages"][-1]["verification"]
    path.write_text(json.dumps(on_disk, indent=2, ensure_ascii=False), encoding="utf-8")

    reloaded = _monitor(tmp_path)
    assert reloaded.load_history() is True
    record = reloaded.message_history[-1]
    assert "verification" not in record
    # The reader at group_handler.py:1326, unchanged.
    assert record.get("verification", "legacy") == "legacy"
