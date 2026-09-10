"""The node ledger keeps every row it was handed, and only ever adds.

`append_jsonl` rotates at 5 MB by renaming the file to `.1` and deleting the
previous `.1`; a financial record cannot be kept that way. The ledger writes
one partition per month under `<DPC_HOME>/ledger/` and never renames or
deletes one. A writer killed mid-line leaves one torn line: the next append
starts on a fresh line, and the reader skips what it cannot parse (ADR-041 D3).
"""

import logging
import os
import pathlib
import time
from datetime import datetime, timezone

import pytest

from dpc_client_core import node_ledger
from dpc_client_core.node_ledger import NodeLedger, usage_row

# Captured at import, before the autouse fixture in conftest points
# `ledger_dir` at the test's tmp_path.
_LEDGER_DIR_UNPATCHED = node_ledger.ledger_dir

AUGUST = datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 1, 0, 0, 1, tzinfo=timezone.utc)


def _row(started_at, **overrides):
    fields = dict(
        request_id="req-1", caller="agent_001", caller_kind="agent",
        alias="ds_flash", model="deepseek-v4-flash", route="local",
        prompt_tokens=1000, completion_tokens=900, thinking_tokens=None,
        counts_source="engine", started_at=started_at, duration_s=1.25,
        billing="pay_per_use", cost_usd=0.0041,
    )
    fields.update(overrides)
    return usage_row(**fields)


def test_a_row_lands_in_the_month_it_started(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")

    ledger.append(_row(AUGUST, request_id="in-august"))
    ledger.append(_row(SEPTEMBER, request_id="in-september"))

    assert [p.name for p in ledger.partitions()] == ["usage-2026-08.jsonl", "usage-2026-09.jsonl"]
    assert [r["request_id"] for r in ledger.rows()] == ["in-august", "in-september"]
    assert [r["request_id"] for r in ledger.rows(month="2026-09")] == ["in-september"]


def test_a_torn_last_line_is_skipped_and_cannot_swallow_the_next_row(tmp_path, caplog):
    """A writer killed mid-write leaves `{"request_id": "to` with no newline.
    Without the fresh-line rule the next row would be glued onto it and both
    would be lost as one unparsable line."""
    ledger = NodeLedger(tmp_path / "ledger")
    ledger.append(_row(SEPTEMBER, request_id="before-the-crash"))
    partition = ledger.partition_for(SEPTEMBER.isoformat())
    with partition.open("ab") as f:
        f.write(b'{"request_id": "torn-by-a-kill", "caller": "ag')

    ledger.append(_row(SEPTEMBER, request_id="after-the-restart"))
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.node_ledger"):
        survivors = [r["request_id"] for r in ledger.rows()]

    assert survivors == ["before-the-crash", "after-the-restart"]
    assert partition.read_bytes().count(b"\n") == 3
    skipped = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
    assert len(skipped) == 1 and ":2 " in skipped[0]


def test_nothing_is_ever_renamed_or_deleted_however_large_a_month_grows(tmp_path, monkeypatch):
    """`append_jsonl` would have rotated at 5 MB and deleted the previous `.1`."""
    touched = []

    def _spy(name, original):
        def wrapper(path, *args, **kwargs):
            touched.append((name, pathlib.Path(path).name))
            return original(path, *args, **kwargs)
        return wrapper

    for name in ("rename", "replace", "unlink", "remove"):
        monkeypatch.setattr(os, name, _spy(f"os.{name}", getattr(os, name)))
    for name in ("rename", "replace", "unlink"):
        monkeypatch.setattr(pathlib.Path, name, _spy(f"Path.{name}", getattr(pathlib.Path, name)))

    ledger = NodeLedger(tmp_path / "ledger")
    a_megabyte = "x" * (1024 * 1024)
    for i in range(6):
        ledger.append(_row(SEPTEMBER, request_id=f"big-{i}", caller=a_megabyte))

    (partition,) = ledger.partitions()
    assert partition.stat().st_size > 5 * 1024 * 1024
    assert [r["request_id"] for r in ledger.rows()] == [f"big-{i}" for i in range(6)]
    assert sorted(p.name for p in (tmp_path / "ledger").iterdir()) == ["usage-2026-09.jsonl"]
    assert [t for t in touched if t[1].endswith(".jsonl")] == []


def test_a_stale_lock_is_cleared_and_a_live_one_is_released(tmp_path):
    ledger = NodeLedger(tmp_path / "ledger")
    partition = ledger.partition_for(SEPTEMBER.isoformat())
    partition.parent.mkdir()
    stale = partition.with_name(partition.name + ".lock")
    stale.write_text("")
    long_ago = time.time() - node_ledger.LOCK_STALE_S - 1
    os.utime(stale, (long_ago, long_ago))

    ledger.append(_row(SEPTEMBER))

    assert [r["request_id"] for r in ledger.rows()] == ["req-1"]
    assert not stale.exists()


def test_every_column_of_d3_is_present_in_its_order():
    row = _row(SEPTEMBER, task_id="task-1", conversation_id="conv-1")

    assert list(row) == [
        "request_id", "caller", "caller_kind", "alias", "model", "route",
        "prompt_tokens", "completion_tokens", "thinking_tokens", "counts_source",
        "started_at", "duration_s", "billing", "cost_usd",
        "task_id", "conversation_id",
    ]
    assert row["started_at"] == "2026-09-01T00:00:01+00:00"
    assert "task_id" not in _row(SEPTEMBER) and "conversation_id" not in _row(SEPTEMBER)


def test_the_vocabulary_is_enforced_at_the_row():
    """`gateway` is accepted, because the writer must take the value the later
    child will emit; a word outside D3's lists is refused rather than written."""
    assert _row(SEPTEMBER, caller_kind="gateway")["caller_kind"] == "gateway"
    for bad in (
        dict(caller_kind="stranger"), dict(route="hub"),
        dict(counts_source="theirs"), dict(billing="free"),
    ):
        with pytest.raises(ValueError):
            _row(SEPTEMBER, **bad)
    with pytest.raises(ValueError):
        _row(datetime(2026, 9, 1))  # naive: the hour that prices the call is unknown


def test_the_default_directory_follows_the_nodes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("DPC_HOME", str(tmp_path / "elsewhere"))

    assert _LEDGER_DIR_UNPATCHED() == tmp_path / "elsewhere" / "ledger"


def test_a_partition_is_byte_identical_on_every_platform(tmp_path):
    """Measured on Windows before this test existed: os.open without O_BINARY
    hands the newline to the CRT, which writes CRLF, so a partition written on
    one node was not the same bytes as one written on another. The ledger is a
    record that travels; its bytes may not depend on the OS that wrote them."""
    from datetime import datetime, timezone
    from dpc_client_core.node_ledger import NodeLedger, usage_row

    ledger = NodeLedger(tmp_path / "ledger")
    row = usage_row(
        request_id="r1", caller="a", caller_kind="agent", alias="x", model="m",
        route="local", prompt_tokens=1, completion_tokens=1, thinking_tokens=None,
        counts_source="ours", started_at=datetime.now(timezone.utc), duration_s=0.1,
        billing="subscription", cost_usd=0.0,
    )
    path = ledger.append(row)
    ledger.append(row)
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert raw.count(b"\n") == 2 and raw.endswith(b"\n")


def test_a_row_nobody_priced_says_null_not_zero(tmp_path, caplog):
    """`float(None or 0.0)` turned "not counted" into "free". A null stays
    null through the file, and a pay-per-use row without a price is logged:
    that is a price that should have been computed."""
    ledger = NodeLedger(tmp_path / "ledger")
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.node_ledger"):
        unpriced = _row(SEPTEMBER, request_id="unpriced", billing="pay_per_use", cost_usd=None)
        free = _row(SEPTEMBER, request_id="free", billing="subscription", cost_usd=None)
    ledger.append(unpriced)
    ledger.append(free)

    assert unpriced["cost_usd"] is None and free["cost_usd"] is None
    assert [r["cost_usd"] for r in ledger.rows()] == [None, None]
    warned = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "unpriced" in warned[0]


def test_spent_today_sums_this_callers_rows_on_this_alias_for_the_utc_day(tmp_path):
    """The first reader of the ledger (ADR-041 D5): a vendor alias's daily
    ceiling is per caller, so the day's spend is the sum over rows carrying
    this alias *and* this caller, on the UTC calendar day, and a null cost —
    a call nobody priced — adds nothing rather than raising."""
    ledger = NodeLedger(tmp_path / "ledger")
    noon = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    just_before_midnight = datetime(2026, 9, 9, 23, 59, 59, tzinfo=timezone.utc)
    ledger.append(_row(noon, request_id="ours-1", caller="us", caller_kind="gateway", cost_usd=0.25))
    ledger.append(_row(noon, request_id="ours-2", caller="us", caller_kind="gateway", cost_usd=0.5))
    ledger.append(_row(noon, request_id="ours-peer", caller="us", caller_kind="peer", cost_usd=4.0))
    ledger.append(_row(noon, request_id="theirs", caller="them", caller_kind="gateway", cost_usd=8.0))
    ledger.append(_row(noon, request_id="other-alias", caller="us", caller_kind="gateway", alias="ds_pro", cost_usd=16.0))
    ledger.append(_row(just_before_midnight, request_id="yesterday", caller="us", caller_kind="gateway", cost_usd=32.0))
    ledger.append(_row(noon, request_id="unpriced", caller="us", caller_kind="gateway", cost_usd=None))

    assert ledger.spent_today("ds_flash", caller="us", caller_kind="gateway", now=noon) == pytest.approx(0.75)
    assert ledger.spent_today("ds_flash", caller="us", now=noon) == pytest.approx(4.75)
    assert ledger.spent_today("ds_flash", caller="nobody", now=noon) == 0.0
    assert ledger.spent_today("ds_flash", caller="us", caller_kind="gateway", now=just_before_midnight) == pytest.approx(32.0)
