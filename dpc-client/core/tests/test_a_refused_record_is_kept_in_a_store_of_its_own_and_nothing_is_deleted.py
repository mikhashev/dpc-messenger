"""A refused record is kept in a store of its own, and nothing is deleted.

A group record this node refuses — wrong signature, or no signature the
`reject_unsigned` setting may check — used to leave one log line and nothing
else. The store tested here (`dpc_client_core.rejected_records.py`) keeps
every refusal as one row under `<dpc home>/state/`, counts repeats instead of
appending them again, and moves the oldest rows to an append-only archive when
the caps are past — the live file is rewritten only after the archive line is
on disk, so a crash between the two loses nothing.

Nothing here goes through a handler: this file is the store's own behaviour.
The live path and the merge path have their own files
(test_a_refused_live_record_is_kept_and_shown_and_never_enters_history.py,
test_a_record_refused_by_a_merge_is_kept_with_the_node_that_delivered_it.py).
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from dpc_client_core import rejected_records
from dpc_client_core.rejected_records import RejectedRecordStore

GROUP = "group-abc123"
OTHER = "group-other"
NODE_A = "dpc-node-" + "a" * 32
NODE_B = "dpc-node-" + "b" * 32

# The row's shape, keys and order both — it is also the UI wire shape, so a
# renamed key is a broken UI, not a refactor.
ENTRY_KEYS = [
    "key", "conversation_id", "record_id", "reason", "transport_node_id",
    "claimed_sender_node_id", "claimed_sender_name", "claimed_sender_type",
    "claimed_agent_owner", "signer_node_id", "claimed_content_hash",
    "computed_content_hash", "signature", "preimage_version", "record_timestamp",
    "first_seen", "last_seen", "repeat_count", "live_count", "sync_count",
    "repair_count", "text", "text_truncated",
]


class Clock:
    """An injectable `now` the test moves by hand, so `last_seen` is exact."""

    def __init__(self, start: datetime):
        self.now = start.astimezone(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: float = 1.0) -> datetime:
        self.now = self.now + timedelta(minutes=minutes)
        return self.now


def _a_refusal(**overrides):
    """The keyword arguments of one plain refusal, overridable per test."""
    kwargs = dict(
        conversation_id=GROUP,
        reason="signer_mismatch",
        arrived="live",
        transport_node_id=NODE_A,
        record_id="m-1",
        claimed_content_hash="hash-claimed",
        computed_content_hash="hash-computed",
        record_timestamp="2026-11-28T00:00:00+00:00",
        claimed_sender_node_id=NODE_B,
        claimed_sender_name="Mike (linux)",
        claimed_sender_type="human",
        claimed_agent_owner=None,
        signer_node_id=NODE_A,
        signature="sig",
        preimage_version="dptp-msg-v3",
        text="the refused text",
    )
    kwargs.update(overrides)
    return kwargs


def _live_rows(store):
    with open(store.file_path, encoding="utf-8") as handle:
        return json.load(handle)["records"]


def _archive_lines(store):
    archive = store.file_path.with_name(rejected_records.ARCHIVE_NAME)
    if not archive.exists():
        return []
    return [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines() if line]


def test_a_record_is_kept_with_every_field_and_only_in_its_own_folder(tmp_path):
    path = tmp_path / "state" / rejected_records.FILE_NAME
    store = RejectedRecordStore(path)

    entry, is_new = store.record(**_a_refusal(), now=Clock(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)))

    assert is_new is True
    assert path.exists(), "the live file was created on the first record kept"
    assert list(entry) == ENTRY_KEYS, "the row's keys are the UI wire shape, order included"
    rows = _live_rows(store)
    assert len(rows) == 1
    assert list(rows[0]) == ENTRY_KEYS
    kept = rows[0]
    assert kept["conversation_id"] == GROUP
    assert kept["record_id"] == "m-1"
    assert kept["reason"] == "signer_mismatch"
    assert kept["transport_node_id"] == NODE_A
    assert kept["claimed_sender_node_id"] == NODE_B
    assert kept["claimed_sender_name"] == "Mike (linux)"
    assert kept["claimed_sender_type"] == "human"
    assert kept["claimed_agent_owner"] is None
    assert kept["signer_node_id"] == NODE_A
    assert kept["claimed_content_hash"] == "hash-claimed"
    assert kept["computed_content_hash"] == "hash-computed"
    assert kept["signature"] == "sig"
    assert kept["preimage_version"] == "dptp-msg-v3"
    assert kept["record_timestamp"] == "2026-11-28T00:00:00+00:00"
    assert kept["first_seen"] == "2026-11-28T12:00:00+00:00"
    assert kept["last_seen"] == "2026-11-28T12:00:00+00:00"
    assert kept["repeat_count"] == 1
    assert kept["live_count"] == 1
    assert kept["sync_count"] == 0
    assert kept["repair_count"] == 0
    assert kept["text"] == "the refused text"
    assert kept["text_truncated"] is False
    assert kept["key"] and isinstance(kept["key"], str)
    # Nothing was created in any other folder.
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written == [path]


def test_the_same_refusal_again_is_counted_not_appended(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)
    clock = Clock(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))

    first, _ = store.record(**_a_refusal(), now=clock)
    clock.tick()
    second, is_new = store.record(**_a_refusal(), now=clock)

    assert is_new is False
    rows = _live_rows(store)
    assert len(rows) == 1, "a repeat is counted on the row it already made"
    assert rows[0]["repeat_count"] == 2
    assert rows[0]["first_seen"] == first["first_seen"], "the first sighting does not move"
    assert rows[0]["last_seen"] == clock().isoformat(), "the last one does"
    assert second["repeat_count"] == 2


def test_the_same_record_from_another_delivering_node_is_its_own_row(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    store.record(**_a_refusal(), now=Clock(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)))
    store.record(**_a_refusal(transport_node_id=NODE_B),
                 now=Clock(datetime(2026, 11, 28, 12, 1, tzinfo=timezone.utc)))

    rows = _live_rows(store)
    assert len(rows) == 2
    assert {row["transport_node_id"] for row in rows} == {NODE_A, NODE_B}


def test_once_live_once_sync_from_the_same_node_is_one_row_counted_both_ways(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    store.record(**_a_refusal(arrived="live"),
                 now=Clock(datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)))
    store.record(**_a_refusal(arrived="sync"),
                 now=Clock(datetime(2026, 11, 28, 12, 1, tzinfo=timezone.utc)))
    store.record(**_a_refusal(arrived="repair"),
                 now=Clock(datetime(2026, 11, 28, 12, 2, tzinfo=timezone.utc)))

    rows = _live_rows(store)
    assert len(rows) == 1
    assert rows[0]["live_count"] == 1
    assert rows[0]["sync_count"] == 1
    assert rows[0]["repair_count"] == 1
    assert rows[0]["repeat_count"] == 3


def test_the_text_is_cut_at_the_limit_and_marked_a_missing_one_is_empty(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    long_text = "x" * (rejected_records.TEXT_LIMIT + 50)
    store.record(**_a_refusal(text=long_text), now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))
    store.record(**_a_refusal(record_id="m-short", text="short"),
                 now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))
    store.record(**_a_refusal(record_id="m-none", text=None),
                 now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))

    by_id = {row["record_id"]: row for row in _live_rows(store)}
    assert by_id["m-1"]["text"] == "x" * rejected_records.TEXT_LIMIT
    assert by_id["m-1"]["text_truncated"] is True
    assert by_id["m-short"]["text"] == "short"
    assert by_id["m-short"]["text_truncated"] is False
    assert by_id["m-none"]["text"] == ""
    assert by_id["m-none"]["text_truncated"] is False


def test_a_delivering_node_holds_no_more_than_its_cap_and_a_repeat_pushes_nothing_out(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)
    clock = Clock(datetime(2026, 11, 28, tzinfo=timezone.utc))

    for i in range(rejected_records.PER_NODE_CAP + 1):
        clock.tick()
        store.record(**_a_refusal(record_id=f"m-{i}"), now=clock)

    rows = _live_rows(store)
    assert len(rows) == rejected_records.PER_NODE_CAP, "the live file holds the cap, no more"
    archived = _archive_lines(store)
    assert len(archived) == 1, "the one row past the cap moved, whole, to the archive"
    assert archived[0]["record_id"] == "m-0", "the node's oldest row is the one that moved"
    assert all(row["record_id"] != "m-0" for row in rows)

    # A repeat of a row still live is counted and evicts nothing.
    clock.tick()
    store.record(**_a_refusal(record_id=f"m-{rejected_records.PER_NODE_CAP}"), now=clock)
    assert len(_live_rows(store)) == rejected_records.PER_NODE_CAP
    assert len(_archive_lines(store)) == 1


def test_the_total_cap_moves_the_oldest_row_whatever_node_delivered_it(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME, total_cap=5)
    clock = Clock(datetime(2026, 11, 28, tzinfo=timezone.utc))

    for i, node in enumerate([NODE_A, NODE_B] * 3):  # six arrivals, alternating nodes
        clock.tick()
        store.record(**_a_refusal(record_id=f"m-{i}", transport_node_id=node), now=clock)

    rows = _live_rows(store)
    assert len(rows) == 5, "the total cap holds"
    archived = _archive_lines(store)
    assert len(archived) == 1
    assert archived[0]["record_id"] == "m-0", "the oldest row overall moved, across nodes"
    assert archived[0]["transport_node_id"] == NODE_A
    assert [row["record_id"] for row in rows] == [f"m-{i}" for i in range(1, 6)]


def test_the_archive_line_lands_before_the_live_file_is_rewritten(tmp_path):
    """The one ordering that must never flip: archive first, live rewrite after."""
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME, total_cap=1)
    clock = Clock(datetime(2026, 11, 28, tzinfo=timezone.utc))

    store.record(**_a_refusal(), now=clock)

    def refusing_write(path, records):
        raise RuntimeError("the live writer broke")

    original = rejected_records._write_live_file
    rejected_records._write_live_file = refusing_write
    try:
        clock.tick()
        with pytest.raises(RuntimeError):
            store.record(**_a_refusal(record_id="m-2"), now=clock)
    finally:
        rejected_records._write_live_file = original

    archived = _archive_lines(store)
    assert [row["record_id"] for row in archived] == ["m-1"], (
        "the row the cap moved is in the archive although the rewrite failed"
    )


def test_an_unreadable_live_file_is_moved_aside_not_overwritten(tmp_path):
    path = tmp_path / "state" / rejected_records.FILE_NAME
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")

    store = RejectedRecordStore(path)
    assert store.for_conversation(GROUP) == [], "a file that cannot be parsed reads as empty"

    moved = list((tmp_path / "state").glob("rejected_records.unreadable-*.json"))
    assert len(moved) == 1, "the unreadable file was renamed, not lost"
    assert moved[0].read_text(encoding="utf-8") == "{ this is not json"
    assert not path.exists()

    # And the store works on from there.
    store.record(**_a_refusal(), now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))
    rows = _live_rows(store)
    assert [row["record_id"] for row in rows] == ["m-1"]
    assert len(list((tmp_path / "state").glob("rejected_records.unreadable-*.json"))) == 1


def test_an_unknown_reason_or_arrival_is_refused_before_anything_is_written(tmp_path):
    path = tmp_path / "state" / rejected_records.FILE_NAME
    store = RejectedRecordStore(path)

    with pytest.raises(ValueError):
        store.record(**_a_refusal(reason="because"))
    with pytest.raises(ValueError):
        store.record(**_a_refusal(arrived="yesterday"))
    assert not path.exists(), "a bad call wrote nothing"
    assert list(tmp_path.rglob("*")) == [], "nor created any folder"


def test_a_second_store_on_the_same_path_sees_the_rows_of_its_conversation_only(tmp_path):
    path = tmp_path / "state" / rejected_records.FILE_NAME
    first = RejectedRecordStore(path)
    first.record(**_a_refusal(), now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))
    first.record(**_a_refusal(conversation_id=OTHER, record_id="m-other"),
                 now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))

    second = RejectedRecordStore(path)
    ours = second.for_conversation(GROUP)
    assert [row["record_id"] for row in ours] == ["m-1"]
    assert second.for_conversation(OTHER)[0]["record_id"] == "m-other"
    assert second.for_conversation("group-not-held") == []


def test_a_signature_check_row_holds_no_exception_text(tmp_path):
    """The reason is a code, never a raw exception string — there is no field
    an exception could hide in, and the key set says so twice."""
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    entry, _ = store.record(**_a_refusal(reason="signature_check_failed"),
                            now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))

    assert list(entry) == ENTRY_KEYS
    assert entry["reason"] == "signature_check_failed"
    dumped = json.dumps(entry)
    assert "RuntimeError" not in dumped and "Exception" not in dumped and "Traceback" not in dumped


def test_the_default_path_and_the_process_wide_store(tmp_path, monkeypatch):
    monkeypatch.setattr(rejected_records, "DPC_HOME_DIR", tmp_path)
    assert rejected_records.default_path() == tmp_path / "state" / rejected_records.FILE_NAME

    rejected_records.set_store(None)
    a = rejected_records.get_store()
    assert rejected_records.get_store() is a, "the store is process-wide, built once per home"
    assert a.file_path == tmp_path / "state" / rejected_records.FILE_NAME

    monkeypatch.setattr(rejected_records, "DPC_HOME_DIR", tmp_path / "moved")
    b = rejected_records.get_store()
    assert b is not a and b.file_path == tmp_path / "moved" / "state" / rejected_records.FILE_NAME

    rejected_records.set_store(None)
    c = rejected_records.get_store()
    assert c is not b and c.file_path == tmp_path / "moved" / "state" / rejected_records.FILE_NAME

    explicit = RejectedRecordStore(tmp_path / "elsewhere" / rejected_records.FILE_NAME)
    rejected_records.set_store(explicit)
    assert rejected_records.get_store() is explicit
    rejected_records.set_store(None)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX mode bits to read")
def test_the_archive_is_created_private_like_the_live_file(tmp_path):
    """The archive holds the same refused texts as the 0600 live file, so a
    plain `open` (the umask's mode) is not private enough for it."""
    import stat

    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME, total_cap=1)
    clock = Clock(datetime(2026, 11, 28, tzinfo=timezone.utc))

    for i in range(2):
        clock.tick()
        store.record(**_a_refusal(record_id=f"m-{i}"), now=clock)

    archive = store.file_path.with_name(rejected_records.ARCHIVE_NAME)
    assert archive.exists(), "one row past the cap moved, so the archive exists"
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600, (
        "the archive is this node's alone, like the live file"
    )


def test_the_archive_only_grows_and_never_rewrites_a_line(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME, total_cap=1)
    clock = Clock(datetime(2026, 11, 28, tzinfo=timezone.utc))

    for i in range(3):
        clock.tick()
        store.record(**_a_refusal(record_id=f"m-{i}"), now=clock)

    archive = store.file_path.with_name(rejected_records.ARCHIVE_NAME)
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "two rows past the cap, two lines, nothing merged"
    assert json.loads(lines[0])["record_id"] == "m-0"
    # A later move leaves the earlier line byte for byte as it was.
    clock.tick()
    store.record(**_a_refusal(record_id="m-3"), now=clock)
    assert archive.read_text(encoding="utf-8").splitlines()[0] == lines[0]


def test_a_missing_live_file_reads_as_empty_and_creates_nothing(tmp_path):
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    assert store.for_conversation(GROUP) == []
    assert not store.file_path.exists(), "reading creates nothing"


def test_a_record_carrying_an_unpaired_surrogate_is_kept_anyway(tmp_path):
    """On the wire a lone surrogate is the JSON escape \\ud800; `json.loads`
    turns it into a lone surrogate inside the Python str, which utf-8 cannot
    encode. A forger put one in a refused record's text and the red row
    vanished: the write raised, the handler swallowed it, and the refusal was
    neither kept nor shown. Every string that enters a row is sanitised to
    U+FFFD first, so the escape costs the forger nothing."""
    dirty_text = json.loads('"a \\ud800 b"')
    dirty_id = json.loads('"id-\\ud800"')
    dirty_name = json.loads('"Mike \\ud83d"')
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    entry, is_new = store.record(
        **_a_refusal(text=dirty_text, record_id=dirty_id, claimed_sender_name=dirty_name),
        now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)),
    )

    assert is_new is True
    assert store.file_path.exists(), "the live file was written despite the surrogate"
    rows = _live_rows(store)  # re-parsed from disk: the file holds no surrogate
    assert len(rows) == 1
    assert rows[0]["text"] == "a \ufffd b"
    assert rows[0]["record_id"] == "id-\ufffd"
    assert rows[0]["claimed_sender_name"] == "Mike \ufffd"
    for value in rows[0].values():
        if isinstance(value, str):
            assert not any(0xD800 <= ord(cp) <= 0xDFFF for cp in value)
    assert entry["text"] == "a \ufffd b", "the copy the event carries is clean too"


def test_an_astral_character_survives_the_sanitiser_unchanged(tmp_path):
    """A Python str holds an astral character as one code point, so an emoji is
    not a surrogate and must come through byte for byte."""
    emoji_text = "\U0001F600 grin and \U0001F680 rocket"
    store = RejectedRecordStore(tmp_path / "state" / rejected_records.FILE_NAME)

    store.record(**_a_refusal(text=emoji_text, record_id="m-emoji"),
                 now=Clock(datetime(2026, 11, 28, tzinfo=timezone.utc)))

    rows = _live_rows(store)
    assert rows[0]["text"] == emoji_text
    assert rows[0]["text"].encode("utf-8") == emoji_text.encode("utf-8")


def test_many_threads_share_one_store(tmp_path, monkeypatch):
    """`get_store` builds the process-wide store under a lock: two threads
    asking at once must not each build a copy of one file, or a write is lost."""
    import threading

    monkeypatch.setattr(rejected_records, "DPC_HOME_DIR", tmp_path)
    rejected_records.set_store(None)

    barrier = threading.Barrier(8)
    grabbed = []

    def grab():
        barrier.wait()
        grabbed.append(rejected_records.get_store())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(grabbed) == 8
    assert all(store is grabbed[0] for store in grabbed)
    assert rejected_records.get_store() is grabbed[0]
    rejected_records.set_store(None)
