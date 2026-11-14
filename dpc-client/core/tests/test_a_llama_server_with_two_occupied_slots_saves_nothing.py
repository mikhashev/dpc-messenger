"""llama-server's `/slots` names no conversation, so the save takes the one slot holding tokens
(ADR-044, amendment 2026-11-13, point 2): none, nothing is live and nothing is said; more than
one (two calls ran side by side), nothing is saved and the skip is logged, because which slot
is whose cannot be told. The stop goes on either way.
"""

import logging

import pytest

from dpc_client_core.managers import kv_snapshots
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import stop_by_hand

from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    card,
    index_entries,
    kv_state,
    note_call,
    saving_pair,
    settings,
    swap_on,
)


@pytest.mark.asyncio
async def test_two_occupied_slots_save_nothing_and_say_why(tmp_path, caplog):
    holder, _new, fake, folder = await saving_pair(
        tmp_path, slots=[{"id": 0, "n_prompt_tokens": 9000}, {"id": 1, "n_prompt_tokens": 1200}])
    note_call()

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        outcome = await stop_by_hand(holder)

    assert fake.calls == [] and holder._proc is None and index_entries(folder) == []
    assert outcome["outcome"] == base_mod._KV_SKIPPED
    assert outcome["reason"] == "2 slots are occupied, so which is whose cannot be told"
    assert any("not saved: 2 slots are occupied" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_one_occupied_slot_is_the_one_saved_whichever_it_is(tmp_path):
    holder, _new, fake, folder = await saving_pair(
        tmp_path, slots=[{"id": 0, "n_prompt_tokens": 0}, {"id": 1, "n_prompt_tokens": 1200}])
    note_call()

    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == kv_snapshots.SAVED
    assert [(slot, action) for slot, action, _name, _t in fake.calls] == [(1, "save")]


@pytest.mark.asyncio
async def test_no_occupied_slot_is_nothing_live(tmp_path):
    holder, _new, fake, folder = await saving_pair(
        tmp_path, slots=[{"id": 0, "n_prompt_tokens": 0}, {"id": 1, "n_prompt_tokens": 0}])
    note_call()

    assert await stop_by_hand(holder) is None
    assert fake.calls == [] and holder._proc is None


@pytest.mark.asyncio
async def test_slots_that_cannot_be_read_save_nothing_and_the_stop_goes_on(tmp_path):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    note_call()

    async def unreadable():
        raise ConnectionError("no answer on /slots")

    holder.slots = unreadable

    outcome = await stop_by_hand(holder)

    assert fake.calls == [] and holder._proc is None
    assert outcome["reason"].startswith("the slots could not be read")
