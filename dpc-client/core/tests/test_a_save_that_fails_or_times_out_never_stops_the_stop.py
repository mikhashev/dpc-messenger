"""A KV snapshot save never blocks a stop and never fails one (ADR-044, amendment 2026-11-13,
point 4): a save that raises, hangs past its bound, is refused or loses the engine is caught
inside the save step, logged in its own words, and the stop goes on. A swap still serves its
request: `_stop_for_the_swap` reads any exception out of `drain` as a failed stop.
"""

import logging

import httpx
import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import card_holders, stop_by_hand

from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    ALIAS,
    card,
    index_entries,
    kv_state,
    note_call,
    saving_pair,
    settings,
    swap_on,
)


@pytest.mark.asyncio
async def test_a_swap_whose_save_raises_still_stops_the_holder_and_serves_the_request(tmp_path):
    holder, new, fake, _folder = await saving_pair(tmp_path)
    note_call()

    async def broken(save_by):
        raise RuntimeError("index on fire")

    holder._save_live_conversation = broken

    props = await new.ensure_running()

    assert props == {"total_slots": 1}
    assert holder._proc is None and [h["alias"] for h in card_holders()] == ["flash"]
    saved = base_mod.last_swap()["live_saved"]
    assert saved["outcome"] == kv_snapshots.FAILED and "index on fire" in saved["reason"]


@pytest.mark.asyncio
async def test_a_save_that_hangs_is_cut_at_its_bound_and_the_stop_goes_on(tmp_path, monkeypatch):
    monkeypatch.setattr(kv_snapshots, "KV_SNAPSHOT_WRITE_CEILING_S", 0.2)
    holder, _new, fake, folder = await saving_pair(tmp_path, delay=30.0)
    note_call()

    outcome = await stop_by_hand(holder)

    assert fake.events == ["save", "stop"] and holder._proc is None
    assert outcome["outcome"] == kv_snapshots.FAILED
    assert outcome["reason"] == "the save ran past its bound of 0.2 s"
    assert outcome["bytes"] is None and outcome["ms"] < 5_000
    assert index_entries(folder) == [] and not list(folder.glob("*.staging"))


@pytest.mark.asyncio
async def test_a_save_that_loses_the_connection_is_logged_and_the_stop_goes_on(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path, raises=httpx.ConnectError("refused"))
    note_call()

    outcome = await stop_by_hand(holder)

    assert holder._proc is None and outcome["outcome"] == kv_snapshots.FAILED
    assert kv_snapshots.off_reason(ALIAS) is None  # not read as the engine refusing saves
    assert index_entries(folder) == []


@pytest.mark.asyncio
async def test_a_llama_server_500_is_a_failed_save_and_never_suspends_saving(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path, status=500,
                                                   body={"error": {"message": "Unable to save slot"}})
    note_call()

    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == kv_snapshots.FAILED and outcome["reason"] == "failed (500)"
    assert kv_snapshots.off_reason(ALIAS) is None and holder._proc is None


@pytest.mark.asyncio
async def test_a_request_fault_warns_in_its_own_words_each_launch_and_leaves_saving_on(tmp_path, caplog):
    holder, _new, fake, folder = await saving_pair(tmp_path, status=415, body="Unsupported Media Type")

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        note_call()
        first = await stop_by_hand(holder)
        await holder.ensure_running()
        note_call()
        second = await stop_by_hand(holder)

    assert first["outcome"] == second["outcome"] == kv_snapshots.TRANSPORT
    assert "(415)" in first["reason"] and kv_snapshots.off_reason(ALIAS) is None
    assert len(fake.calls) == 2  # saving stayed on: the second stop tried again
    lines = [r for r in caplog.records if "live conversation not saved" in r.getMessage()]
    # Once per launch, and each stop ends its launch: the next launch warns again.
    assert [r.levelno for r in lines] == [logging.WARNING, logging.WARNING]
    assert all("application/json" in r.getMessage() for r in lines)


@pytest.mark.asyncio
async def test_an_engine_refusal_suspends_saving_for_the_launch_only(tmp_path, monkeypatch):
    """The Strata rule on the base path: a 500 classified as the engine refusing saves."""
    real = kv_snapshots.classify_save
    monkeypatch.setattr(kv_snapshots, "classify_save",
                        lambda code, body=None, *, engine="strata": real(code, body, engine="strata"))
    holder, _new, fake, folder = await saving_pair(tmp_path, status=500, body={"error": {"message": "x"}})
    note_call()

    first = await stop_by_hand(holder)
    assert first["outcome"] == kv_snapshots.ENGINE_REFUSES
    assert kv_snapshots.off_reason(ALIAS) == kv_snapshots.ENGINE_REFUSES_SAVES

    await holder.ensure_running()  # a new launch clears the suspension
    assert kv_snapshots.off_reason(ALIAS) is None
    note_call()
    await stop_by_hand(holder)
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_after_a_refusal_the_same_launch_saves_no_more(tmp_path, monkeypatch):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    kv_snapshots.suspend(ALIAS, kv_snapshots.ENGINE_REFUSES_SAVES)
    note_call()

    outcome = await stop_by_hand(holder)

    assert fake.calls == [] and holder._proc is None
    assert outcome["outcome"] == base_mod._KV_SKIPPED
    assert outcome["reason"] == f"suspended for this launch: {kv_snapshots.ENGINE_REFUSES_SAVES}"


@pytest.mark.asyncio
async def test_a_slow_ledger_read_counts_against_the_bound(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(kv_snapshots, "KV_SNAPSHOT_WRITE_CEILING_S", 0.2)
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()

    async def slow_cold_rate(tokens):
        await asyncio.sleep(30)

    holder._kv_cold_rate = slow_cold_rate

    outcome = await stop_by_hand(holder)

    assert fake.calls == [] and holder._proc is None
    assert outcome["reason"] == "the save ran past its bound of 0.2 s"


@pytest.mark.asyncio
async def test_no_time_left_skips_before_the_engine_is_asked_anything(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()

    outcome = await stop_by_hand(holder, save_by=base_mod._swap_clock() + 5.0)  # less than the 20 s stop

    assert fake.slot_reads == 0 and fake.calls == [] and holder._proc is None
    assert outcome["reason"] == "no time left before the request's stop bound"


@pytest.mark.asyncio
async def test_a_save_cancelled_midway_still_stops_the_child_and_the_alias_serves_again(tmp_path):
    import asyncio

    holder, _new, fake, folder = await saving_pair(tmp_path, delay=30.0)
    note_call()
    stopping = asyncio.ensure_future(stop_by_hand(holder))
    while not fake.calls:
        await asyncio.sleep(0.01)

    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert fake.events == ["save", "stop"] and holder._proc is None
    assert holder._draining is False and not list(folder.glob("*.staging"))
