"""The last engine call is the call sent last (ADR-044, 2026-11-13, points 2 and 7).

One record per alias. The provider's `_ensure` notes a call (`note_engine_call`,
pending) and `call_slot` marks it sent at the last synchronous step before the
request goes on the wire (`mark_sent`); only a sent call becomes the record, and
the latest sent wins. A peer's call noted first but delayed by an await, then sent
after the owner's, is what the slot holds, so it is the record (review F1). The
order is a counter taken at `mark_sent`, never the wall clock (15.6 ms ticks on
Windows, NTP steps); `at` is kept for display only. A cleared history deletes the
conversation's snapshots in every registered folder and marks it, so the next
drain does not save the same slot again; the mark lasts until that conversation's
next call reaches the engine.
"""

import threading
from pathlib import Path

import pytest

from dpc_client_core.managers import kv_snapshots as kv

NOW = 3_000_000.0


@pytest.fixture(autouse=True)
def _clean_state():
    kv.reset_state()
    yield
    kv.reset_state()


def _sent(alias, **kw):
    kw.setdefault("at", NOW)
    seq = kv.note_engine_call(alias, **kw)
    assert kv.mark_sent(alias, seq)
    return seq


def test_a_noted_call_is_pending_and_never_the_record_until_sent():
    seq = kv.note_engine_call("flash", caller_kind="agent", agent_id="a", conversation_id="c", at=NOW)
    assert isinstance(seq, int)
    assert kv.engine_call_of("flash") is None
    assert not kv.note_engine_tokens("flash", seq, 10, 2)
    assert kv.mark_sent("flash", seq)
    assert kv.engine_call_of("flash")["conversation_id"] == "c"
    assert not kv.mark_sent("flash", seq)                      # already sent
    assert not kv.mark_sent("flash", 10 ** 9)                  # unknown
    assert not kv.mark_sent("other", seq)                      # another alias's number


def test_the_call_sent_last_wins_not_the_call_noted_last():
    owner = kv.note_engine_call("flash", caller_kind="agent", agent_id="a", conversation_id="c", at=NOW)
    peer = kv.note_engine_call("flash", caller_kind="peer", peer_id="dpc-node-x", at=NOW + 1)
    # Noted later but on the wire first: the owner's call reached the slot last.
    assert kv.mark_sent("flash", peer)
    assert kv.mark_sent("flash", owner)
    record = kv.engine_call_of("flash")
    assert record["caller_kind"] == "agent" and record["seq"] == owner
    # The review's case: the peer noted first, delayed by an await, sent last.
    early_peer = kv.note_engine_call("flash", caller_kind="peer", peer_id="p", at=NOW + 2)
    late_owner = kv.note_engine_call("flash", caller_kind="agent", conversation_id="c", at=NOW + 3)
    kv.mark_sent("flash", late_owner)
    kv.mark_sent("flash", early_peer)
    assert kv.engine_call_of("flash")["caller_kind"] == "peer"


def test_the_record_keeps_the_monotonic_time_it_was_sent(monkeypatch):
    clock = iter([100.0, 250.0])
    monkeypatch.setattr(kv.time, "monotonic", lambda: next(clock))
    seq = kv.note_engine_call("flash", caller_kind="agent", conversation_id="c", at=NOW)
    assert kv.mark_sent("flash", seq)
    assert kv.engine_call_of("flash")["sent_mono"] == 100.0
    _sent("flash", caller_kind="peer", peer_id="p")
    assert kv.engine_call_of("flash")["sent_mono"] == 250.0


def test_a_cancelled_or_refused_call_never_becomes_the_record():
    _sent("flash", caller_kind="agent", conversation_id="owner")
    kv.note_engine_call("flash", caller_kind="peer", peer_id="p", at=NOW + 1)     # refused in call_slot
    assert kv.engine_call_of("flash")["conversation_id"] == "owner"


def test_the_wall_clock_plays_no_part_in_the_order():
    first = _sent("flash", caller_kind="agent", conversation_id="c", at=NOW + 5)
    second = _sent("flash", caller_kind="peer", peer_id="p", at=NOW + 1)        # clock stepped back
    assert second > first
    record = kv.engine_call_of("flash")
    assert record["caller_kind"] == "peer" and record["at"] == NOW + 1
    assert kv.note_engine_tokens("flash", second, 10, 2) and not kv.note_engine_tokens("flash", first, 10, 2)


def test_pending_calls_are_bounded_per_alias():
    seqs = [kv.note_engine_call("flash", caller_kind="agent", conversation_id=f"c{i}", at=NOW)
            for i in range(kv.PENDING_PER_ALIAS + 10)]
    assert not kv.mark_sent("flash", seqs[0])                   # dropped as the oldest
    assert kv.mark_sent("flash", seqs[-1])
    assert len(kv._PENDING["flash"]) == kv.PENDING_PER_ALIAS - 1


def test_a_dropped_pending_call_is_warned_once_per_alias_per_launch(caplog):
    def flood(alias, n):
        for i in range(n):
            kv.note_engine_call(alias, caller_kind="agent", conversation_id=f"c{i}", at=NOW)

    def drops(alias):
        return [r for r in caplog.records
                if r.levelname == "WARNING" and "dropped a pending call" in r.getMessage() and alias in r.getMessage()]

    with caplog.at_level("WARNING", logger=kv.logger.name):
        flood("flash", kv.PENDING_PER_ALIAS)
        assert drops("flash") == []                             # at the cap, nothing dropped yet
        flood("flash", 20)
        flood("qwen", kv.PENDING_PER_ALIAS + 1)
        assert len(drops("flash")) == 1 and len(drops("qwen")) == 1
        kv.launch_began("flash")
        flood("flash", 1)
        assert len(drops("flash")) == 2                         # a new launch warns again
        assert len(drops("qwen")) == 1


def test_records_are_per_alias_and_copies():
    _sent("a", caller_kind="gateway")
    assert kv.engine_call_of("b") is None
    copy = kv.engine_call_of("a")
    copy["caller_kind"] = "changed"
    assert kv.engine_call_of("a")["caller_kind"] == "gateway"


def test_the_record_carries_the_prefix_digest():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    _sent("flash", caller_kind=kv.OWNER_CHAT_KIND, conversation_id="c", prefix=kv.prefix_digest(messages))
    record = kv.engine_call_of("flash")
    assert record["prefix_messages"] == 2
    assert kv.matches(record, messages + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}])
    assert not kv.matches(record, [{"role": "system", "content": "s"}, {"role": "user", "content": "cut"}])
    assert not kv.matches(record, messages[:1])


def test_the_digest_is_canonical_and_bounded_by_k():
    a = [{"role": "user", "content": "x", "name": "n"}]
    b = [{"name": "n", "content": "x", "role": "user"}]
    assert kv.prefix_digest(a) == kv.prefix_digest(b)
    assert kv.prefix_digest(a + b, 1) == kv.prefix_digest(a)
    assert kv.prefix_digest([], None)[1] == 0


def test_tokens_land_only_on_the_call_that_is_still_the_record():
    seq = _sent("flash", caller_kind="agent", conversation_id="c")
    assert kv.note_engine_tokens("flash", seq, 1_000, 200)
    assert kv.engine_call_of("flash")["prompt_tokens"] == 1_000
    pending = kv.note_engine_call("flash", caller_kind="agent", conversation_id="c", at=NOW)
    assert kv.note_engine_tokens("flash", seq, 1_001, 201)      # a pending call does not displace it
    kv.mark_sent("flash", pending)
    assert not kv.note_engine_tokens("flash", seq, 9_999, 9)
    record = kv.engine_call_of("flash")
    assert record["prompt_tokens"] is None and record["reply_tokens"] is None
    assert not kv.note_engine_tokens("nobody", seq, 1, 1)


def test_the_owner_kinds_are_agent_and_the_owners_chat_and_the_four_words_are_kept():
    assert kv.is_owner_kind("agent") and kv.is_owner_kind(kv.OWNER_CHAT_KIND)
    for kind in ("peer", "gateway", None):
        assert not kv.is_owner_kind(kind)
    for kind in ("agent", "peer", "gateway", kv.OWNER_CHAT_KIND, None):
        _sent("flash", caller_kind=kind, conversation_id="c")
        assert kv.engine_call_of("flash")["caller_kind"] == kind


def test_an_unknown_caller_kind_is_recorded_as_none_with_no_conversation(caplog):
    kv.forget_conversation("c1")
    with caplog.at_level("WARNING", logger=kv.logger.name):
        for _ in range(3):
            seq = _sent("flash", caller_kind="agnet", agent_id="a", conversation_id="c1")
    record = kv.engine_call_of("flash")
    assert record["seq"] == seq
    assert record["caller_kind"] is None and record["conversation_id"] is None and record["agent_id"] is None
    assert not kv.is_owner_kind(record["caller_kind"])
    assert kv.is_cleared("c1")                       # a record with no conversation lifts no mark
    assert sum("agnet" in r.getMessage() for r in caplog.records) == 1


def test_concurrent_sends_leave_the_last_sent():
    order = []
    lock = threading.Lock()

    def call(i):
        seq = kv.note_engine_call("flash", caller_kind="agent", conversation_id=f"c{i}", at=NOW)
        with lock:                                   # the wire order, as call_slot would see it
            kv.mark_sent("flash", seq)
            order.append(i)

    threads = [threading.Thread(target=call, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert kv.engine_call_of("flash")["conversation_id"] == f"c{order[-1]}"


# ---- a cleared history


def _save(folder: Path, alias: str, conversation: str, agent="ag"):
    folder.mkdir(exist_ok=True)
    _, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"s")
    record = {"agent_id": agent, "conversation_id": conversation, "caller_kind": "agent"}
    kv.add_entry(folder, kv.new_entry(file=final, alias=alias, engine="strata", identity={}, record=record,
                                      n_saved=1, nbytes=1, save_ms=1, now=NOW), cap_bytes=10 ** 6)
    return final


def test_forgetting_a_conversation_deletes_it_in_every_registered_folder_and_marks_it(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    a1 = _save(one, "flash", "c1")
    a2 = _save(one, "flash", "c1", agent="other-agent")
    b1 = _save(two, "qwen", "c1")
    keep = _save(two, "qwen", "c2")
    kv.register_folder("flash", one)
    kv.register_folder("qwen", two)
    removed = kv.forget_conversation("c1")
    assert sorted(removed) == sorted([(a1, kv.CLEARED), (a2, kv.CLEARED), (b1, kv.CLEARED)])
    assert [e["file"] for e in kv.read_index(one)["entries"]] == []
    assert [e["file"] for e in kv.read_index(two)["entries"]] == [keep]
    assert kv.is_cleared("c1") and not kv.is_cleared("c2")


def test_the_cleared_mark_is_lifted_only_when_the_conversations_call_is_sent(tmp_path):
    kv.forget_conversation("c1")
    assert kv.is_cleared("c1")
    _sent("flash", caller_kind="agent", conversation_id="c2")
    assert kv.is_cleared("c1")
    seq = kv.note_engine_call("flash", caller_kind="agent", conversation_id="c1", at=NOW - 100)
    assert kv.is_cleared("c1")                       # noted is not sent
    assert kv.mark_sent("flash", seq)
    assert not kv.is_cleared("c1")
    assert kv.engine_call_of("flash")["conversation_id"] == "c1"


def test_a_conversation_is_clearable_only_once_its_monitor_noted_it():
    assert not kv.is_clearable("c1")
    kv.note_clearable("c1")
    assert kv.is_clearable("c1") and not kv.is_clearable("c2")
    kv.note_clearable("c1")                          # a second monitor for the same id
    assert kv.is_clearable("c1")
    kv.note_clearable(None)
    kv.note_clearable("")
    assert not kv.is_clearable(None) and not kv.is_clearable("")
    kv.reset_state()                                 # in memory only: a restart forgets it
    assert not kv.is_clearable("c1")


def test_clearable_notes_from_many_threads_all_land():
    threads = [threading.Thread(target=kv.note_clearable, args=(f"c{i}",)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(kv.is_clearable(f"c{i}") for i in range(50))


def test_forgetting_never_raises_on_a_missing_disk_and_ignores_no_id(tmp_path):
    kv.register_folder("flash", tmp_path / "unplugged")
    assert kv.forget_conversation("c1") == []
    assert kv.is_cleared("c1")
    assert kv.forget_conversation(None) == [] and kv.forget_conversation("") == []
    kv.unregister_folder("flash")
    assert kv.folder_of("flash") is None


def test_forgetting_runs_beside_a_save_from_another_thread(tmp_path):
    kv.register_folder("flash", tmp_path)
    names = []
    errors = []

    def saver():
        try:
            for i in range(10):
                names.append(_save(tmp_path, "flash", f"keep{i}"))
        except Exception as e:  # pragma: no cover - the assertion below names it
            errors.append(e)

    def forgetter():
        for _ in range(10):
            kv.forget_conversation("c1")

    _save(tmp_path, "flash", "c1")
    threads = [threading.Thread(target=saver), threading.Thread(target=forgetter)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(e["file"] for e in kv.read_index(tmp_path)["entries"]) == sorted(names)


def test_the_status_row_counts_the_registered_folder(tmp_path):
    _save(tmp_path, "flash", "c1")
    _save(tmp_path, "flash", "c2")
    kv.register_folder("flash", tmp_path, cap_bytes=kv.cap_bytes_of({}))
    kv.suspend("flash", kv.FOLDER_MISSING)
    kv.note_restore("flash", {"restored": True})
    status = kv.status_of("flash", now=NOW)
    assert status["files"] == 2 and status["bytes"] == 2
    assert status["cap_bytes"] == 32 * 1024 ** 3
    assert status["oldest_expiry"] == NOW + kv.DEFAULT_TTL_S
    assert status["off_reason"] == "folder missing"
    assert status["last_restore"] == {"restored": True} and status["last_save"] is None
    assert kv.status_of("unknown")["files"] == 0


def test_the_status_row_counts_only_this_aliass_unexpired_entries(tmp_path):
    _save(tmp_path, "flash", "c1")
    _save(tmp_path, "other-alias", "c2")
    kv.register_folder("flash", tmp_path)
    assert kv.status_of("flash", now=NOW)["files"] == 1
    later = NOW + kv.DEFAULT_TTL_S + 1
    status = kv.status_of("flash", now=later)
    assert status["files"] == 0 and status["bytes"] == 0 and status["oldest_expiry"] is None
