"""A switch saves nothing where the engine's RAM keeps the outgoing conversation (ADR-044, amendments
of 2026-11-18: the Strata switch contract point 1 and "Settled by Mike" Q-A, and the slot queue as a
scheduler, decision 1).

On llama-server `cache_ram_mib` not 0 (or unset, which is the engine's own host cache) means the
outgoing conversation stays in RAM at a switch, so the incoming call must not wait for a write the
RAM makes useless; the stop save and the restore gate are untouched. At `cache_ram_mib` 0 the
switch save is unconditional, as before.

Fakes and helpers are those of the switch test file.
"""

import logging

import pytest

from dpc_client_core.managers import kv_snapshots as kv

from tests.test_one_local_engine_holds_the_card import card  # noqa: F401 (the autouse card fixture)
from tests.test_a_kv_snapshot_is_saved_and_restored_at_an_agent_switch import (  # noqa: F401
    SUP_LOG, TURN_2, ALIAS, entries, kv_state, lines, snapshot_of, switching, turn,
)

RAM_WORDS = "the engine keeps it in RAM (host cache, cache_ram"


@pytest.mark.asyncio
async def test_with_a_host_cache_the_switch_sends_the_incoming_call_without_a_save(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=24576)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2")
    assert engine.events == ["request:c1", "request:c2"]
    assert entries(folder) == []
    assert sup._in_flight == 0 and not sup._kv_switch_lock.locked()
    decision, = lines(caplog, "KV snapshot decision")
    # B is an owner call: the restore step says the switch's one line, with the RAM reason as the no-save words.
    assert ("KV snapshot decision at a switch from agent a1 conversation c1 to an agent call "
            "(agent a2 conversation c2): no save (" + RAM_WORDS + " 24576); a stop writes it)") in decision


@pytest.mark.asyncio
async def test_a_switch_to_a_call_with_nothing_to_restore_says_not_saved_at_the_switch(tmp_path, caplog):
    """A background call has no conversation to restore: the hook says the switch's one line itself."""
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=24576)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await p.generate_response("summarise the day")
    assert engine.events == ["request:c1", "request:background"] and entries(folder) == []
    decision, = lines(caplog, "KV snapshot decision")
    assert ("KV snapshot decision at a switch from agent a1 conversation c1 to a background call: "
            "not saved at the switch: " + RAM_WORDS + " 24576); a stop writes it") in decision
    assert sup._in_flight == 0 and not sup._kv_switch_lock.locked()


@pytest.mark.asyncio
async def test_the_incoming_call_does_not_wait_for_a_write_under_a_host_cache(tmp_path):
    """The save gate would hold a save forever; B still goes out."""
    import asyncio

    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=24576)
    await turn(p, "a1", "c1")
    engine.save_gate = asyncio.Event()
    await asyncio.wait_for(turn(p, "a2", "c2"), timeout=5)
    assert "save" not in engine.events and engine.events[-1] == "request:c2"


@pytest.mark.asyncio
async def test_an_unset_cache_ram_is_the_engines_own_host_cache_and_saves_nothing_at_a_switch(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=None)  # the default config value
    assert sup.config.get("cache_ram_mib") is None
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2")
    assert engine.events == ["request:c1", "request:c2"] and entries(folder) == []
    decision, = lines(caplog, "KV snapshot decision")
    assert RAM_WORDS + " default); a stop writes it" in decision


@pytest.mark.asyncio
async def test_with_no_host_cache_the_switch_still_saves_the_outgoing_slot(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=0)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2")
    assert engine.events == ["request:c1", "save", "save-end", "request:c2"]
    entry, = entries(folder)
    assert (entry["agent_id"], entry["conversation_id"]) == ("a1", "c1")
    decision, = lines(caplog, "KV snapshot decision")
    assert "saving (" in decision and "keeps it in RAM" not in decision


@pytest.mark.asyncio
async def test_under_a_host_cache_the_restore_gates_decide_as_before_when_nothing_was_saved(tmp_path, caplog):
    """Slot 0 holds the outgoing call, which no snapshot holds: the restore's own guard refuses, and the
    switch still has one decision line (the RAM one)."""
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=24576)
    final = snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert engine.events == ["request:c1", "request:c2"]
    assert lines(caplog, "KV snapshot of agent a2 conversation c2 not restored at a switch")
    assert "which no snapshot holds" in lines(caplog, "KV snapshot of agent a2 conversation c2 not restored")[0]
    decision, = lines(caplog, "KV snapshot decision")
    assert "no save (" + RAM_WORDS + " 24576)" in decision
    assert [e["file"] for e in entries(folder)] == [final]


@pytest.mark.asyncio
async def test_under_a_host_cache_an_empty_slot_lets_the_incoming_snapshot_restore_with_no_save(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=24576)
    snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    engine.slot_list[0]["n_prompt_tokens"] = 0  # nothing is live in the slot
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert engine.events == ["request:c1", "restore", "request:c2"]
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.RESTORED
    decision, = lines(caplog, "KV snapshot decision")
    assert RAM_WORDS + " 24576)" in decision


@pytest.mark.asyncio
async def test_the_host_cache_gate_still_refuses_a_key_this_launch_served_after_its_snapshot(tmp_path, caplog):
    """The restore side is unchanged: B comes back after a call its snapshot was not saved from."""
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=24576)
    await turn(p, "a2", "c2")
    snapshot_of(p, folder, "a2", "c2")  # older than the key's last sent call: the host cache may hold more
    await turn(p, "a1", "c1")
    engine.slot_list[0]["n_prompt_tokens"] = 0
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert "restore" not in engine.events
    assert lines(caplog, "the host cache (cache_ram not 0) may hold a newer copy")
