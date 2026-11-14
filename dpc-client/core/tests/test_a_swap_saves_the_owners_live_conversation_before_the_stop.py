"""A swap saves the owner's live conversation to a KV snapshot before the stop (ADR-044,
amendment 2026-11-13, points 3, 4 and 10).

Real supervisors on the fakes of the swap tests; the engine's `/slots` and its slot save are
faked at the supervisor's own seams (`slots`, `_slot_action`), and the fake save writes the
file the engine would. The other KV snapshot test files of the supervisor import the helpers
here.
"""

import asyncio
import logging
import time

import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import card_holders, stop_by_hand
from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

from tests.test_an_owners_start_swaps_out_an_idle_local_engine_that_holds_the_card import (  # noqa: F401
    settings,
    swap_on,
)
from tests.test_one_local_engine_holds_the_card import _llama, _strata, card  # noqa: F401

ALIAS = "qwen27"


@pytest.fixture(autouse=True)
def kv_state(monkeypatch):
    """Records and outcomes are module state; a tmp disk is never short here."""
    kv_snapshots.reset_state()
    monkeypatch.setattr(kv_snapshots, "disk_short", lambda folder, nbytes: False)
    monkeypatch.setattr(base_mod, "_last_caller_call_at", {})
    yield
    kv_snapshots.reset_state()


class FakeEngine:
    """`/slots` and the slot save of one llama-server child."""

    def __init__(self, sup, folder, slots=None, status=200, body=None, size=4096, delay=0.0,
                 raises=None, events=None):
        self.folder = folder
        self.slot_list = [{"id": 0, "n_prompt_tokens": 1500}, {"id": 1, "n_prompt_tokens": 0}] \
            if slots is None else slots
        self.status, self.body, self.size = status, body, size
        self.delay, self.raises = delay, raises
        self.events = events if events is not None else []
        self.calls = []
        self.slot_reads = 0
        sup.slots = self.slots
        sup._slot_action = self.action

        async def no_cold_rows(tokens):
            return None, 0

        sup._kv_cold_rate = no_cold_rows

    async def slots(self):
        self.slot_reads += 1
        return self.slot_list

    async def action(self, slot, action, filename, timeout):
        self.calls.append((slot, action, filename, timeout))
        self.events.append(action)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        if self.status == 200 and action == "save":
            (self.folder / filename).write_bytes(b"\0" * self.size)
        return self.status, self.body if self.body is not None else {"n_saved": 1500, "n_written": self.size}


PREFIX = ("ab" * 32, 3)


def note_call(alias=ALIAS, kind="agent", agent="a1", conversation="c1", peer=None, at=None, prefix=PREFIX):
    """A call noted and sent to the engine, as the provider and `call_slot` do."""
    note_clearable = getattr(kv_snapshots, "note_clearable", None)
    if note_clearable is not None and conversation:
        note_clearable(conversation)  # a conversation monitor holds it, as for a chat or an agent's turn
    seq = kv_snapshots.note_engine_call(alias, caller_kind=kind, agent_id=agent, conversation_id=conversation,
                                        peer_id=peer, prefix=prefix, at=time.time() if at is None else at)
    assert kv_snapshots.mark_sent(alias, seq)


def saving_llama(tmp_path, folder=None):
    folder = folder or tmp_path / "kv"
    return _llama(ALIAS, slot_save_path=str(folder)), folder


def watch_stops(sup, events):
    real = sup._stop_child

    async def stop_child():
        events.append("stop")
        await real()

    sup._stop_child = stop_child


async def saving_pair(tmp_path, **engine):
    """A started llama-server holder with a KV snapshots folder, a not-started Strata rival."""
    holder, folder = saving_llama(tmp_path)
    new = _strata(tmp_path, "flash")
    await holder.ensure_running()
    _ACTIVE_SUPERVISORS[holder.alias] = holder
    _ACTIVE_SUPERVISORS[new.alias] = new
    events = []
    fake = FakeEngine(holder, folder, events=events, **engine)
    watch_stops(holder, events)
    return holder, new, fake, folder


def index_entries(folder):
    return kv_snapshots.read_index(folder)["entries"]


@pytest.mark.asyncio
async def test_the_swap_saves_the_live_slot_then_stops_the_holder(tmp_path, caplog):
    holder, new, fake, folder = await saving_pair(tmp_path)
    note_call()

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    assert fake.events == ["save", "stop"]
    assert holder._proc is None and [h["alias"] for h in card_holders()] == ["flash"]
    (slot, action, staging, _timeout), = fake.calls
    assert (slot, action) == (0, "save") and staging.endswith(".staging")
    entry, = index_entries(folder)
    assert (entry["alias"], entry["agent_id"], entry["conversation_id"]) == (ALIAS, "a1", "c1")
    assert entry["file"].endswith(".bin") and (folder / entry["file"]).stat().st_size == 4096
    assert entry["bytes"] == 4096 and entry["n_saved"] == 1500 and entry["caller_kind"] == "agent"
    assert not list(folder.glob("*.staging"))
    saved = base_mod.last_swap()["live_saved"]
    assert saved["outcome"] == kv_snapshots.SAVED and saved["bytes"] == 4096 and saved["ms"] >= 0
    assert any("saved 4096 bytes in" in r.getMessage() and "conversation c1" in r.getMessage()
               for r in caplog.records)
    assert kv_snapshots.status_of(ALIAS)["last_save"] == saved


@pytest.mark.asyncio
async def test_the_save_is_bounded_by_what_the_waiting_request_has_left_less_the_stop(tmp_path, settings):
    settings.set_local_engine_swap(True, 30)
    holder, new, fake, _folder = await saving_pair(tmp_path)
    note_call()

    await new.ensure_running()

    (_slot, _action, _name, timeout), = fake.calls
    # 30 s of the request's wait, less the 20 s a stop is given before one is measured.
    assert 5.0 < timeout <= 10.0


@pytest.mark.asyncio
async def test_a_stop_engine_save_has_the_ceiling_alone(tmp_path):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call()

    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == kv_snapshots.SAVED
    # The timeout is what is left of the ceiling when the write starts: a few ms
    # under it on a loaded machine, never over it.
    ceiling = kv_snapshots.KV_SNAPSHOT_WRITE_CEILING_S
    assert ceiling - 2.0 <= fake.calls[0][3] <= ceiling
    assert fake.events == ["save", "stop"]


@pytest.mark.asyncio
async def test_a_settings_save_that_drains_the_child_saves_too(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call(conversation="chat-1")

    outcome = await holder.drain(timeout=None)

    assert outcome["outcome"] == kv_snapshots.SAVED
    assert [e["conversation_id"] for e in index_entries(folder)] == ["chat-1"]


@pytest.mark.asyncio
async def test_a_newer_save_of_the_same_conversation_replaces_the_older(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    await stop_by_hand(holder)
    first, = index_entries(folder)

    await holder.ensure_running()
    note_call()
    # The first save measured the folder: a seed-sized estimate would not fit the bound.
    kv_snapshots.write_index(folder, {**kv_snapshots.read_index(folder), "rates": {}, "sizes": {}})
    await stop_by_hand(holder)

    second, = index_entries(folder)
    assert second["file"] != first["file"] and not (folder / first["file"]).exists()


@pytest.mark.asyncio
async def test_a_gateway_hold_keeps_its_callers_name_for_the_last_call_record():
    async with base_mod.card_held_for(None, "accepted_request", caller="dsh") as hold:
        assert hold.caller == "dsh" and base_mod.current_card_hold().caller == "dsh"
    async with base_mod.card_held_for(None, "accepted_request", agent_id="a1", conversation_id="c1") as hold:
        assert hold.caller is None


class TestTheRestoreHelper:
    """`_restore_kv_snapshot`, which the provider's `_ensure` calls behind its own gate."""

    async def _saved(self, tmp_path):
        holder, _new, fake, folder = await saving_pair(tmp_path)
        note_call()
        await stop_by_hand(holder)
        await holder.ensure_running()
        assert holder._kv_engine_fresh is True
        entry, = index_entries(folder)
        return holder, fake, folder, entry

    @pytest.mark.asyncio
    async def test_a_restore_goes_into_slot_0_and_keeps_the_file(self, tmp_path):
        holder, fake, folder, entry = await self._saved(tmp_path)

        outcome = await holder._restore_kv_snapshot(entry)

        assert fake.calls[-1][:3] == (0, "restore", entry["file"])
        assert outcome["outcome"] == kv_snapshots.RESTORED and outcome["bytes"] == entry["bytes"]
        assert (folder / entry["file"]).exists() and index_entries(folder) == [entry]
        assert kv_snapshots.status_of(ALIAS)["last_restore"] == outcome

    @pytest.mark.asyncio
    async def test_a_snapshot_the_engine_refuses_is_deleted(self, tmp_path):
        holder, fake, folder, entry = await self._saved(tmp_path)
        fake.status, fake.body = 400, {"error": {"message": "x", "kind": "invalid"}}

        outcome = await holder._restore_kv_snapshot(entry)

        assert outcome["outcome"] == kv_snapshots.REFUSED
        assert not (folder / entry["file"]).exists() and index_entries(folder) == []

    @pytest.mark.asyncio
    async def test_no_room_in_the_engine_keeps_the_snapshot(self, tmp_path):
        holder, fake, folder, entry = await self._saved(tmp_path)
        fake.status, fake.body = 400, "No available space in KV cache"

        outcome = await holder._restore_kv_snapshot(entry)

        assert outcome["outcome"] == kv_snapshots.NO_ROOM and (folder / entry["file"]).exists()

    @pytest.mark.asyncio
    async def test_a_restore_under_the_providers_own_call_slot_goes_ahead(self, tmp_path):
        holder, fake, folder, entry = await self._saved(tmp_path)

        async with holder.call_slot():
            outcome = await holder._restore_kv_snapshot(entry)

        assert outcome["outcome"] == kv_snapshots.RESTORED

    @pytest.mark.asyncio
    async def test_a_request_fault_at_restore_warns_once_per_launch_and_keeps_the_file(self, tmp_path, caplog):
        holder, fake, folder, entry = await self._saved(tmp_path)
        fake.status, fake.body = 415, "Unsupported Media Type"

        with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
            first = await holder._restore_kv_snapshot(entry)
            await holder._restore_kv_snapshot(entry)

        assert first["outcome"] == kv_snapshots.TRANSPORT and (folder / entry["file"]).exists()
        lines = [r for r in caplog.records if "KV snapshot of conversation" in r.getMessage()]
        assert [r.levelno for r in lines] == [logging.WARNING, logging.INFO]

    @pytest.mark.asyncio
    async def test_a_draining_engine_restores_nothing(self, tmp_path):
        holder, fake, folder, entry = await self._saved(tmp_path)
        calls = len(fake.calls)
        holder._draining = True

        outcome = await holder._restore_kv_snapshot(entry)

        assert outcome["outcome"] == base_mod._KV_SKIPPED and len(fake.calls) == calls


@pytest.mark.asyncio
async def test_a_snapshot_carries_what_shapes_the_engines_state(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    await stop_by_hand(holder)

    entry, = index_entries(folder)
    assert (entry["n_ctx"], entry["cache_type_k"], entry["cache_type_v"]) == (262144, "q8_0", "q8_0")
    assert entry["mmproj"] is None and entry["gguf_path"] == holder.config["gguf_path"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"n_ctx": 131072}, {"cache_type_k": "q4_0", "cache_type_v": "q4_0"},
                                    {"mmproj": "D:/models/mmproj.gguf"}])
async def test_a_changed_configuration_sweeps_its_old_snapshots_before_the_launch(tmp_path, change):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    await stop_by_hand(holder)
    entry, = index_entries(folder)

    holder.config.update(change)
    await holder.ensure_running()

    assert index_entries(folder) == [] and not (folder / entry["file"]).exists()


@pytest.mark.asyncio
async def test_the_same_configuration_keeps_its_snapshot_across_a_launch(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()
    await stop_by_hand(holder)

    await holder.ensure_running()

    assert len(index_entries(folder)) == 1


@pytest.mark.asyncio
async def test_the_decision_line_says_how_thin_the_cold_rate_is(tmp_path, caplog):
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call()

    async def two_rows(tokens):
        return 1805.0, 2

    holder._kv_cold_rate = two_rows

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await stop_by_hand(holder)

    line, = [r.getMessage() for r in caplog.records if "KV snapshot decision" in r.getMessage()]
    assert "1500 tokens" in line and "cold rate 1805 tok/s on 2 ledger rows" in line
    assert "estimate " in line and "of the bound left" in line and "saving (" in line


@pytest.mark.asyncio
async def test_the_ledger_hook_reads_rows_and_counts_them(tmp_path):
    """The base `_kv_cold_rate` over the test's ledger: no rows, rate and count unknown-or-zero."""
    holder, _folder = saving_llama(tmp_path)

    rate, rows = await holder._kv_cold_rate(20_000)

    assert rate is None and rows in (0, None)
