"""A digest is a promise: every hash in it can be asked for and delivered.

When a peer's file lands, this node writes a note about it — attributed to the
peer, signed by us. `export_history` holds that note back, because every other
node refuses that shape on arrival. The digest counted it anyway, into the
peer's own author bucket, so the peer saw one content hash it lacked, asked for
exactly that one, and was answered with zero messages. The comparison then
reported the same difference on every connection, and no exchange could ever
close it.

One predicate now governs both sides. These tests hold the two together: what
the two nodes advertise must agree, and every hash advertised must be
exportable.
"""

import json
from pathlib import Path

from dpc_client_core.conversation_monitor import ConversationMonitor, digest_for

ME = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
GROUP = "group-b88b65076b85"
FILE = {"type": "image", "filename": "paste.png", "size_bytes": 98889, "hash": "3aea5d86"}


def _signed(mid, author, ts, content, attachments=None, signer=None):
    record = {
        "id": mid,
        "role": "user",
        "content": content,
        "timestamp": ts,
        "sender_node_id": author,
        "sender_name": author[:12],
        "signer_node_id": signer or author,
        "content_hash": f"hash-of-{mid}",
        "signature": f"sig-of-{mid}",
        "preimage_version": "dptp-msg-v2",
    }
    if attachments is not None:
        record["attachments"] = attachments
    return record


def _mine():
    return _signed("mine-1", ME, "2026-09-08T13:45:00Z", "look at this")


def _theirs():
    return _signed("theirs-1", PEER, "2026-09-08T13:46:09Z",
                   "Sent screenshot: paste.png (0.09 MB)", [FILE])


def _note():
    """Ours about their file: theirs by name, ours by key."""
    return _signed("note-1", PEER, "2026-09-08T13:46:11Z",
                   "Received screenshot: paste.png (0.09 MB)", [FILE], signer=ME)


def _monitor(messages, participants):
    monitor = ConversationMonitor.__new__(ConversationMonitor)
    monitor.message_history = messages
    monitor.message_ids = {m["id"] for m in messages}
    monitor.participants = participants
    monitor._local_file_notes = set()
    return monitor


def _roster(local):
    return [
        {"node_id": local, "name": "self", "context": "local"},
        {"node_id": PEER if local == ME else ME, "name": "other", "context": None},
    ]


# --- the two nodes have to advertise the same thing -------------------------

def test_the_two_nodes_agree_although_only_one_holds_the_note():
    """The peer never received the note and never can, so it must not count."""
    ours = _monitor([_mine(), _theirs(), _note()], _roster(ME))
    theirs = _monitor([_mine(), _theirs()], _roster(PEER))

    assert ours.history_digest() == theirs.history_digest()
    assert ours.authors_that_differ(theirs.history_digest()) == []


def test_the_free_function_and_the_method_answer_alike():
    """`GROUP_HISTORY_STATUS` computes the digest one way when the monitor is
    loaded and the other when it is not; the two must not drift apart."""
    ours = _monitor([_mine(), _theirs(), _note()], _roster(ME))

    assert ours.history_digest() == digest_for(list(ours.message_history), ME)
    assert digest_for([_mine(), _theirs()], PEER) == ours.history_digest()


def test_every_hash_the_digest_names_comes_back_from_the_export():
    """The property that makes a difference closable rather than permanent."""
    ours = _monitor([_mine(), _theirs(), _note()], _roster(ME))

    advertised = ours.history_digest()
    wanted = [m["content_hash"] for m in ours.message_history]
    exported = ours.export_history(content_hashes=wanted)

    assert len(exported) == sum(a["count"] for a in advertised["authors"].values())
    assert "hash-of-note-1" not in {m.get("content_hash") for m in exported}


def test_the_peers_own_record_of_the_file_still_counts():
    """The regression half: holding back everything about that file would make
    the peer's real message disappear from the comparison too."""
    ours = _monitor([_mine(), _theirs(), _note()], _roster(ME))

    assert ours.history_digest()["authors"][PEER]["count"] == 1


# --- the free path, which is the usual one on connect -----------------------

def test_the_disk_peek_filters_the_note_as_the_monitor_does(tmp_path, monkeypatch):
    """Monitors are lazy, so on connect the digest is usually computed from a
    plain list read off disk. It has no roster; it reads the identity file."""
    home = tmp_path
    (home / ".dpc").mkdir(parents=True)
    (home / ".dpc" / "node.id").write_text(ME, encoding="utf-8")
    store = home / ".dpc" / "conversations" / GROUP
    store.mkdir(parents=True)
    (store / "history.json").write_text(
        json.dumps({"conversation_id": GROUP, "version": 1,
                    "messages": [_mine(), _theirs(), _note()]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    peeked = ConversationMonitor.peek_group_messages(GROUP)

    assert len(peeked) == 3, "the note stays on disk; only the digest ignores it"
    assert digest_for(peeked) == digest_for([_mine(), _theirs()], PEER)


def _identity(home, node_id=ME):
    (home / ".dpc").mkdir(parents=True, exist_ok=True)
    (home / ".dpc" / "node.id").write_text(node_id, encoding="utf-8")


def test_a_roster_that_marks_no_local_node_reads_the_identity_file(tmp_path, monkeypatch):
    """`_build_participants` marks the local member `context: local` only when
    this node is in `group.members`. Removed from a group, or on any of the
    five Telegram monitors — which carry one participant and no `context` at
    all — the roster names nobody local, and answering with its first entry
    names a peer.
    """
    _identity(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ours = _monitor([_mine(), _theirs(), _note()],
                    [{"node_id": PEER, "name": "other", "context": "peer"}])

    assert ours.history_digest() == digest_for([_mine(), _theirs()], PEER)


def test_an_empty_roster_still_exports_what_it_advertises(tmp_path, monkeypatch):
    """`group.members` is whatever the last GROUP_SYNC carried; nothing
    requires it to be non-empty, and an empty one leaves the roster empty.

    The digest would still filter, because `digest_for` reads the identity file
    when nobody hands it an id — but `_is_local_file_note` has no such default,
    so unless the roster's own fallback reaches the same file, the export ships
    a record the digest never counted, and the two sides part again.
    """
    _identity(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ours = _monitor([_mine(), _theirs(), _note()], [])

    advertised = ours.history_digest()
    exported = ours.export_history(
        content_hashes=[m["content_hash"] for m in ours.message_history]
    )

    assert len(exported) == sum(a["count"] for a in advertised["authors"].values())


def test_with_no_identity_at_all_nothing_is_filtered(tmp_path, monkeypatch):
    """Both halves of the shape are "signed by us" and "not authored by us", so
    with no local node id a peer's genuine record cannot be told from our note
    about it. Fail open: filtering on a guess would drop real history."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    digest = digest_for([_mine(), _theirs(), _note()])

    assert digest["authors"][PEER]["count"] == 2
