"""Spec tests for the last-engine-call record and for forget_conversation.
Written from ADR-044 (amendment 2026-11-13, points 2 and 7), the slice-1 plan
(section 2, P1) and the coordinator's contract changes, without sight of the
implementation.

The record is two-step: note_engine_call creates a pending call and returns its
seq; mark_sent(alias, seq) makes it the alias's record. The record is module-global
state, so every test uses names no other test uses.
"""

import copy
import itertools

import pytest

from dpc_client_core.managers import kv_snapshots as kv

_counter = itertools.count()
HOUR = 3600.0
NOW = 2_000_000.0


@pytest.fixture
def alias():
    return f"spec-alias-{next(_counter)}"


def _get(record, key):
    if isinstance(record, dict):
        return record.get(key)
    return getattr(record, key, None)


def _note(alias, *, conversation_id, at, caller_kind="agent", agent_id="agent-1",
          peer_id=None):
    """Note one call (pending only); returns its sequence number."""
    return kv.note_engine_call(
        alias, caller_kind=caller_kind, agent_id=agent_id,
        conversation_id=conversation_id, peer_id=peer_id,
        prefix=kv.prefix_digest([{"role": "user", "content": "hi"}]), at=at)


def _call(alias, *, conversation_id, at, **kw):
    """Note one call and mark it sent; returns its sequence number."""
    seq = _note(alias, conversation_id=conversation_id, at=at, **kw)
    assert kv.mark_sent(alias, seq) is True
    return seq


def _snap(alias):
    return repr(copy.deepcopy(kv.engine_call_of(alias)))


# ------------------------------------------------------------- last-call record

def test_a_call_note_returns_an_integer_sequence_number_that_grows(alias):
    first = _note(alias, conversation_id="a", at=100.0)
    second = _note(alias, conversation_id="b", at=101.0)
    assert isinstance(first, int) and isinstance(second, int)
    assert second > first


def test_a_noted_call_is_pending_and_not_yet_the_record(alias):
    _note(alias, conversation_id="a", at=100.0)
    assert not kv.engine_call_of(alias)


def test_a_noted_but_never_sent_call_is_never_the_record(alias):
    sent = _call(alias, conversation_id="sent", at=100.0)
    _note(alias, conversation_id="never-sent", at=200.0)   # noted later, never sent

    rec = kv.engine_call_of(alias)
    assert _get(rec, "conversation_id") == "sent"
    assert _get(rec, "seq") == sent


def test_mark_sent_makes_the_pending_call_the_record_and_carries_its_seq(alias):
    seq = _note(alias, conversation_id="a", at=100.0)
    assert kv.mark_sent(alias, seq) is True

    rec = kv.engine_call_of(alias)
    assert _get(rec, "seq") == seq
    assert _get(rec, "conversation_id") == "a"


def test_mark_sent_is_false_for_an_unknown_seq(alias):
    seq = _note(alias, conversation_id="a", at=100.0)
    assert kv.mark_sent(alias, seq + 1000) is False
    assert not kv.engine_call_of(alias)


def test_mark_sent_is_false_the_second_time(alias):
    seq = _note(alias, conversation_id="a", at=100.0)
    assert kv.mark_sent(alias, seq) is True
    assert kv.mark_sent(alias, seq) is False


def test_mark_sent_is_false_for_another_alias(alias):
    seq = _note(alias, conversation_id="a", at=100.0)
    assert kv.mark_sent(alias + "-other", seq) is False
    assert not kv.engine_call_of(alias)
    assert not kv.engine_call_of(alias + "-other")


def test_a_call_noted_first_but_sent_last_wins_over_one_noted_later_but_sent_earlier(alias):
    first = _note(alias, conversation_id="noted-first", at=100.0)
    second = _note(alias, conversation_id="noted-second", at=200.0)

    assert kv.mark_sent(alias, second) is True
    assert _get(kv.engine_call_of(alias), "conversation_id") == "noted-second"

    assert kv.mark_sent(alias, first) is True          # reached the engine last
    rec = kv.engine_call_of(alias)
    assert _get(rec, "conversation_id") == "noted-first"
    assert _get(rec, "seq") == first


def test_the_later_sent_call_wins_even_if_its_at_is_earlier(alias):
    # A clock stepped back: the moment of mark_sent orders the record, not `at`.
    _call(alias, conversation_id="sent-first", at=200.0)
    seq2 = _call(alias, conversation_id="sent-second", at=100.0)

    rec = kv.engine_call_of(alias)
    assert _get(rec, "conversation_id") == "sent-second"
    assert _get(rec, "seq") == seq2


def test_a_later_sent_call_with_a_later_at_replaces_too(alias):
    _call(alias, conversation_id="older", at=100.0)
    _call(alias, conversation_id="newer", at=200.0)

    assert _get(kv.engine_call_of(alias), "conversation_id") == "newer"


def test_the_record_keeps_the_callers_kind_and_peer(alias):
    _call(alias, conversation_id=None, at=50.0, caller_kind="peer", agent_id=None,
          peer_id="dpc-node-" + "a" * 32)
    rec = kv.engine_call_of(alias)
    assert _get(rec, "caller_kind") == "peer"
    assert _get(rec, "peer_id") == "dpc-node-" + "a" * 32


def test_the_owner_chat_kind_is_a_constant_distinct_from_peer_and_gateway(alias):
    assert isinstance(kv.OWNER_CHAT_KIND, str) and kv.OWNER_CHAT_KIND
    assert kv.OWNER_CHAT_KIND not in ("peer", "gateway")

    _call(alias, conversation_id="chat-1", at=10.0, caller_kind=kv.OWNER_CHAT_KIND,
          agent_id=None)
    assert _get(kv.engine_call_of(alias), "caller_kind") == kv.OWNER_CHAT_KIND


def test_two_aliases_keep_separate_records(alias):
    other = alias + "-other"
    _call(alias, conversation_id="mine", at=10.0)
    _call(other, conversation_id="theirs", at=20.0)

    assert _get(kv.engine_call_of(alias), "conversation_id") == "mine"
    assert _get(kv.engine_call_of(other), "conversation_id") == "theirs"


def test_an_alias_with_no_call_has_no_record(alias):
    assert not kv.engine_call_of(alias)


def test_tokens_for_the_current_sent_calls_seq_change_the_record(alias):
    seq = _call(alias, conversation_id="c", at=300.0)
    before = _snap(alias)

    kv.note_engine_tokens(alias, seq, 12_345, 678)

    assert _snap(alias) != before  # positive control: the matching seq is applied


def test_tokens_for_a_pending_seq_are_ignored(alias):
    sent = _call(alias, conversation_id="sent", at=100.0)
    pending = _note(alias, conversation_id="pending", at=200.0)
    before = _snap(alias)

    kv.note_engine_tokens(alias, pending, 99_999, 1)   # not sent: not the record

    assert _snap(alias) == before
    kv.note_engine_tokens(alias, sent, 12_345, 678)    # the sent one still takes them
    assert _snap(alias) != before


def test_tokens_for_a_pending_seq_with_no_record_at_all_are_ignored(alias):
    pending = _note(alias, conversation_id="pending", at=200.0)
    kv.note_engine_tokens(alias, pending, 99_999, 1)
    assert not kv.engine_call_of(alias)


def test_tokens_for_an_older_calls_seq_are_ignored(alias):
    seq_a = _call(alias, conversation_id="A", at=100.0)
    seq_b = _call(alias, conversation_id="B", at=200.0)
    assert seq_a != seq_b
    before = _snap(alias)

    kv.note_engine_tokens(alias, seq_a, 99_999, 1)   # A's usage arrives after B began

    assert _snap(alias) == before
    # and B's own usage is still applied afterwards
    kv.note_engine_tokens(alias, seq_b, 12_345, 678)
    assert _snap(alias) != before


def test_tokens_for_a_seq_that_never_existed_are_ignored(alias):
    seq = _call(alias, conversation_id="c", at=300.0)
    before = _snap(alias)

    kv.note_engine_tokens(alias, seq + 1000, 12_345, 678)

    assert _snap(alias) == before


def test_tokens_for_an_alias_with_no_call_do_not_raise(alias):
    kv.note_engine_tokens(alias, 1, 10, 10)
    assert not kv.engine_call_of(alias)


def test_many_pending_calls_never_sent_do_not_break_the_record(alias):
    sent = _call(alias, conversation_id="sent", at=1.0)
    for i in range(500):                  # pending calls are capped per alias
        _note(alias, conversation_id=f"p{i}", at=2.0 + i)

    assert _get(kv.engine_call_of(alias), "seq") == sent
    fresh = _note(alias, conversation_id="fresh", at=9_999.0)
    assert kv.mark_sent(alias, fresh) is True      # the newest pending survives the cap
    assert _get(kv.engine_call_of(alias), "conversation_id") == "fresh"


# ------------------------------------------------------------ forget_conversation

def _entry(name, *, alias, conversation_id, agent_id="agent-1", saved_at=NOW - 100,
           caller_kind="agent"):
    return {
        "file": name, "alias": alias, "engine": "strata", "engine_version": "0.1.57",
        "model": "model-a", "agent_id": agent_id, "conversation_id": conversation_id,
        "caller_kind": caller_kind, "n_saved": 1000, "bytes": 300, "save_ms": 5,
        "saved_at": saved_at, "expires_at": saved_at + 24 * HOUR,
        "prefix_sha256": "0" * 64, "prefix_messages": 1,
    }


def _put(folder, entry):
    (folder / entry["file"]).write_bytes(b"x" * entry["bytes"])
    kv.add_entry(folder, entry, cap_bytes=10**12)


def _find(folder, e):
    return kv.lookup(folder, alias=e["alias"], agent_id=e["agent_id"],
                     conversation_id=e["conversation_id"], now=NOW)


def _mark_reader():
    """The plan names the cleared-mark but not how it is read; try the likely names."""
    for name in ("is_cleared", "was_cleared", "is_conversation_cleared",
                 "conversation_cleared", "cleared"):
        fn = getattr(kv, name, None)
        if callable(fn):
            return fn
    return None


def test_forget_conversation_removes_its_entries_in_every_registered_folder(tmp_path, alias):
    cid = f"conv-forget-{next(_counter)}"
    keep_cid = f"conv-keep-{next(_counter)}"
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    alias_a, alias_b = alias + "-a", alias + "-b"
    kv.register_folder(alias_a, a_dir)
    kv.register_folder(alias_b, b_dir)
    try:
        gone_a = _entry("dpc-kv-fa000001.bin", alias=alias_a, conversation_id=cid)
        gone_b = _entry("dpc-kv-fb000001.bin", alias=alias_b, conversation_id=cid,
                        agent_id=None, caller_kind=kv.OWNER_CHAT_KIND)
        stay_a = _entry("dpc-kv-fa000002.bin", alias=alias_a, conversation_id=keep_cid)
        for e, folder in ((gone_a, a_dir), (gone_b, b_dir), (stay_a, a_dir)):
            _put(folder, e)

        kv.forget_conversation(cid)

        assert not (a_dir / gone_a["file"]).exists()
        assert not (b_dir / gone_b["file"]).exists()
        assert gone_a["file"] not in (a_dir / kv.INDEX_NAME).read_text(encoding="utf-8")
        assert gone_b["file"] not in (b_dir / kv.INDEX_NAME).read_text(encoding="utf-8")
        assert _find(a_dir, gone_a) is None
        assert _find(b_dir, gone_b) is None
        # another conversation's entry in the same folder is untouched
        assert (a_dir / stay_a["file"]).exists()
        assert _find(a_dir, stay_a) is not None
    finally:
        kv.unregister_folder(alias_a)
        kv.unregister_folder(alias_b)


def test_forget_conversation_leaves_foreign_files_alone(tmp_path, alias):
    cid = f"conv-forget-{next(_counter)}"
    kv.register_folder(alias, tmp_path)
    try:
        foreign = tmp_path / "image-job-session.bin"
        foreign.write_bytes(b"y" * 100)
        e = _entry("dpc-kv-ff000001.bin", alias=alias, conversation_id=cid)
        _put(tmp_path, e)

        kv.forget_conversation(cid)

        assert foreign.exists()
        assert not (tmp_path / e["file"]).exists()
    finally:
        kv.unregister_folder(alias)


def test_forget_conversation_ignores_an_unregistered_folder(tmp_path, alias):
    cid = f"conv-forget-{next(_counter)}"
    e = _entry("dpc-kv-0f0a0001.bin", alias=alias, conversation_id=cid)
    _put(tmp_path, e)   # never registered

    kv.forget_conversation(cid)

    assert (tmp_path / e["file"]).exists()


def test_forget_conversation_with_nothing_registered_does_not_raise():
    kv.forget_conversation(f"conv-nobody-{next(_counter)}")


def test_a_forgotten_conversation_is_marked_until_a_call_of_it_is_sent(alias):
    reader = _mark_reader()
    if reader is None:
        pytest.skip("the plan names the cleared-mark but no function that reads it")
    cid = f"conv-mark-{next(_counter)}"
    _call(alias, conversation_id=cid, at=10.0)
    assert not reader(cid)

    kv.forget_conversation(cid)
    assert reader(cid)

    seq = _note(alias, conversation_id=cid, at=20.0)   # noted only: the mark stays
    assert reader(cid)

    assert kv.mark_sent(alias, seq) is True            # sent: the mark is lifted
    assert not reader(cid)


def test_a_call_of_another_conversation_does_not_clear_the_mark(alias):
    reader = _mark_reader()
    if reader is None:
        pytest.skip("the plan names the cleared-mark but no function that reads it")
    cid = f"conv-mark-{next(_counter)}"
    kv.forget_conversation(cid)
    _call(alias, conversation_id=f"other-{next(_counter)}", at=10.0)
    assert reader(cid)


# ------------------------------------------------------------------ clearable

def test_a_noted_conversation_is_clearable(alias):
    cid = f"conv-clearable-{next(_counter)}"
    kv.note_clearable(cid)
    assert kv.is_clearable(cid)


def test_a_conversation_never_noted_is_not_clearable():
    assert not kv.is_clearable(f"conv-never-noted-{next(_counter)}")


def test_noting_one_conversation_clearable_does_not_make_another_so():
    noted = f"conv-clearable-{next(_counter)}"
    other = f"conv-clearable-{next(_counter)}"
    kv.note_clearable(noted)
    assert kv.is_clearable(noted)
    assert not kv.is_clearable(other)


def test_noting_a_conversation_clearable_twice_is_harmless():
    cid = f"conv-clearable-{next(_counter)}"
    kv.note_clearable(cid)
    kv.note_clearable(cid)
    assert kv.is_clearable(cid)
