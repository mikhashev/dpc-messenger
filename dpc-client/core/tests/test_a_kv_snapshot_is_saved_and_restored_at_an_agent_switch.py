"""A KV snapshot is saved and restored at an agent switch on llama-server (plan
ideas/kv-snapshots-at-an-agent-switch-plan-2026-11-17.md, slice 1; ADR-044, KV snapshot amendment).

Before an incoming call goes out, the provider's hook hands a switch to the supervisor
(`kv_at_a_switch`): when the last sent call was another owner key's, the one occupied slot is saved
through the stop's save path (the hook's own call slot is not "in flight"); then the incoming
owner conversation's snapshot is restored into slot 0. One switch at a time per alias; the save
holds the call slot to its end even when its caller is cancelled, so a drain waits. A peer's call
never waits and nothing is saved for it. Under a host cache (`cache_ram_mib` not 0) a conversation
this launch served is restored only from a snapshot saved from its last sent call (the entry's
`sent` and `sent_mono`), so the host cache cannot hold a longer copy. A 400 restore at a switch keeps the file.

A real supervisor over the fake child of the card tests; `/slots`, the slot save and restore are
faked at the supervisor's own seams, the provider's client by `wire`.
"""

import asyncio
import logging
import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import carrying_card_hold, serving_a_peer

from tests.test_one_local_engine_holds_the_card import _llama, card  # noqa: F401
from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import (
    FakeCompletions, llama, owner_hold, wire,
)

ALIAS = "qwen27"
SYSTEM = "you are a helper"
TURN_1 = [{"role": "user", "content": "list the files"}]
TURN_2 = TURN_1 + [{"role": "assistant", "content": "a.txt b.txt"}, {"role": "user", "content": "open a.txt"}]
SUP_LOG = base_mod.logger.name


@pytest.fixture(autouse=True)
def kv_state(monkeypatch):
    kv.reset_state()
    monkeypatch.setattr(kv, "disk_short", lambda folder, nbytes: False)
    monkeypatch.setattr(base_mod, "_last_caller_call_at", {})
    yield
    kv.reset_state()


class SwitchEngine:
    """Two slots, as the 27B runs (-np 2): a request puts its prompt in slot 0 (the host cache
    took the previous one); a save writes the file; a restore answers `restore_answer`."""

    def __init__(self, sup, folder):
        self.folder = folder
        self.slot_list = [{"id": 0, "n_prompt_tokens": 0}, {"id": 1, "n_prompt_tokens": 0}]
        self.events = []
        self.save_gate = None
        self.save_started = asyncio.Event()
        self.in_flight_at_save = []
        self.restore_answer = None
        sup.slots = self.slots
        sup._slot_action = self.action
        self.sup = sup

        async def no_cold_rows(tokens):
            return None, 0

        sup._kv_cold_rate = no_cold_rows

    async def slots(self):
        return [dict(s) for s in self.slot_list]

    async def action(self, slot, action, filename, timeout):
        self.events.append(action)
        if action == "save":
            self.in_flight_at_save.append(self.sup._in_flight)
            self.save_started.set()
            if self.save_gate is not None:
                await self.save_gate.wait()
            tokens = self.slot_list[slot]["n_prompt_tokens"]
            (self.folder / filename).write_bytes(b"\0" * 4096)
            self.events.append("save-end")
            return 200, {"n_saved": tokens, "n_written": 4096}
        status, body = self.restore_answer or (200, {"n_restored": 777})
        if status == 200:
            self.slot_list[0]["n_prompt_tokens"] = body["n_restored"]
        else:
            self.slot_list[0]["n_prompt_tokens"] = 0  # a failed restore wipes the slot (b12110, 2026-11-17)
        return status, body

    def request(self, params):
        record = kv.engine_call_of(ALIAS)
        self.events.append(f"request:{record['conversation_id'] or record['caller_kind'] or 'background'}")
        self.slot_list[0]["n_prompt_tokens"] = 1000 * len(params["messages"])


async def switching(tmp_path, **cfg):
    folder = tmp_path / "kv"
    sup = _llama(ALIAS, slot_save_path=str(folder), **cfg)
    await sup.ensure_running()
    sup._kv_engine_fresh = False  # the empty-engine restore is not this file's case
    engine = SwitchEngine(sup, folder)
    p = llama(ALIAS)
    completions = FakeCompletions()
    completions.on_create = engine.request
    wire(p, sup=sup, completions=completions)
    return p, sup, engine, folder


async def turn(p, agent, conversation, messages=TURN_1):
    kv.note_clearable(conversation)
    with carrying_card_hold(owner_hold(ALIAS, agent, conversation)):
        return await p.generate_with_tools(messages, [], system=SYSTEM)


def entries(folder):
    return kv.read_index(folder)["entries"]


def lines(caplog, words):
    return [r.getMessage() for r in caplog.records if words in r.getMessage()]


def snapshot_of(p, folder, agent, conversation, messages=TURN_1, size=1024):
    """An indexed snapshot of a conversation this launch never served, as an earlier launch left it.
    The file is `size` zero bytes, extended rather than written."""
    wire_messages = p._anthropic_to_openai_messages(SYSTEM, messages, provider=p)
    sha, k = kv.prefix_digest(wire_messages, kv.base_end(wire_messages))
    staging, final = kv.new_snapshot_names(folder)
    with open(folder / final, "wb") as f:
        f.truncate(size)
    kv.release_staging(folder, staging)
    record = {"agent_id": agent, "conversation_id": conversation, "caller_kind": "agent",
              "prefix_sha256": sha, "prefix_messages": k}
    entry = kv.new_entry(file=final, alias=ALIAS, engine="llamacpp_server", identity={}, record=record,
                         n_saved=777, nbytes=size, save_ms=3, now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=kv.GIB)
    return final


@pytest.mark.asyncio
async def test_an_agent_switch_saves_the_outgoing_slot_before_the_incoming_call_goes_out(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2")
    assert engine.events[:4] == ["request:c1", "save", "save-end", "request:c2"]
    # The hook's own call slot was the only one held, and it is not counted as in flight.
    assert engine.in_flight_at_save == [1]
    assert sup._in_flight == 0 and not sup._kv_switch_lock.locked()
    entry, = entries(folder)
    assert (entry["agent_id"], entry["conversation_id"], entry["n_saved"]) == ("a1", "c1", 2000)
    decision, = lines(caplog, "KV snapshot decision")
    assert ("KV snapshot decision at a switch from agent a1 conversation c1 to an agent call "
            "(agent a2 conversation c2): 2000 tokens, estimate") in decision
    assert "read rate" in decision and "saving (" in decision


@pytest.mark.asyncio
async def test_a_later_call_of_the_same_conversation_is_no_switch(tmp_path):
    """Guard of unchanged behaviour: the slot still holds the conversation, nothing is saved."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    await turn(p, "a1", "c1", TURN_2)
    assert "save" not in engine.events and entries(folder) == []


@pytest.mark.asyncio
async def test_a_background_call_between_a_and_b_saves_a_and_names_its_kind(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await p.generate_response("summarise the day")
        await turn(p, "a2", "c2")
    assert engine.events == ["request:c1", "save", "save-end", "request:background", "request:c2"]
    assert [e["conversation_id"] for e in entries(folder)] == ["c1"]
    assert lines(caplog, "KV snapshot decision at a switch from agent a1 conversation c1 to a background call")
    # B's switch from the background record saves nothing and says why B was not restored.
    assert lines(caplog, "KV snapshot of agent a2 conversation c2 not restored at a switch from "
                         "agent - conversation - to an agent call (agent a2 conversation c2): "
                         "no snapshot of this conversation in the index")


@pytest.mark.asyncio
async def test_a_peer_call_between_a_and_b_saves_nothing_under_an_owner_key_and_says_so(tmp_path, caplog):
    """The slot queue, point 8: a peer's call no longer passes unseen; its switch runs the owner's
    save. With no settings bound its deadline is its arrival, so the save has no time, nothing is
    written, and the eviction line says so."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        with serving_a_peer("dpc-node-peer"):
            await p.generate_response("hello")
        await turn(p, "a2", "c2")
    assert "save" not in engine.events and entries(folder) == []
    assert not lines(caplog, "KV snapshot save not waited for")
    assert lines(caplog, "KV snapshot of agent a1 conversation c1 evicted unsaved by peer dpc-node-peer: "
                         "the save (no time left)")
    assert kv.status_of(ALIAS)["last_save"]["reason"].startswith("no time left")


@pytest.mark.asyncio
async def test_a_drain_that_begins_during_a_switch_save_waits_for_it(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    stops = []
    real_stop_child = sup._stop_child

    async def stop_child():
        stops.append(list(engine.events))
        await real_stop_child()

    sup._stop_child = stop_child
    await turn(p, "a1", "c1")
    engine.save_gate = asyncio.Event()
    b = asyncio.ensure_future(turn(p, "a2", "c2"))
    await asyncio.wait_for(engine.save_started.wait(), timeout=5)
    drain = asyncio.ensure_future(sup.drain(timeout=None))
    for _ in range(10):
        await asyncio.sleep(0.05)
    assert stops == [] and not drain.done()
    engine.save_gate.set()
    await asyncio.wait_for(drain, timeout=10)
    # The switch save ended before the stop; B was refused by the drain or ran first.
    assert stops and "save-end" in stops[0]
    await asyncio.gather(b, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_cancelled_caller_leaves_the_save_running_with_the_slot_and_the_lock(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    engine.save_gate = asyncio.Event()
    b = asyncio.ensure_future(turn(p, "a2", "c2"))
    await asyncio.wait_for(engine.save_started.wait(), timeout=5)
    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b
    # The save goes on and keeps the call slot (a drain waits) and the lock (no second switch).
    assert sup._in_flight == 1 and sup._kv_switch_lock.locked()
    engine.save_gate.set()
    for _ in range(50):
        if sup._in_flight == 0 and not sup._kv_switch_lock.locked():
            break
        await asyncio.sleep(0.01)
    assert sup._in_flight == 0 and not sup._kv_switch_lock.locked()
    entry, = entries(folder)
    assert entry["conversation_id"] == "c1" and not list(folder.glob("*.staging"))
    assert "request:c2" not in engine.events


@pytest.mark.asyncio
async def test_the_incoming_conversation_is_restored_into_slot_0_after_the_save(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert engine.events == ["request:c1", "save", "save-end", "restore", "request:c2"]
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.RESTORED
    assert final in [e["file"] for e in entries(folder)]
    assert sup._in_flight == 0


def served_again(agent, conversation):
    """A call of the key sent after its snapshot, with no save after it (the next save skipped)."""
    seq = kv.note_engine_call(ALIAS, caller_kind="agent", agent_id=agent, conversation_id=conversation,
                              prefix=("cd" * 32, 2), at=time.time())
    assert kv.mark_sent(ALIAS, seq)
    seq = kv.note_engine_call(ALIAS, caller_kind=None, at=time.time())  # then a background call
    assert kv.mark_sent(ALIAS, seq)


@pytest.mark.asyncio
async def test_under_a_host_cache_a_snapshot_saved_after_the_keys_last_call_is_restored(tmp_path):
    """The switch away saved it from its last sent call: the disk copy is at least as new as RAM's."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    await turn(p, "a2", "c2")
    await turn(p, "a1", "c1", TURN_2)
    assert engine.events == ["request:c1", "save", "save-end", "request:c2",
                             "save", "save-end", "restore", "request:c1"]
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.RESTORED


@pytest.mark.asyncio
async def test_under_a_host_cache_a_key_served_again_after_its_save_is_not_restored(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    await turn(p, "a2", "c2")
    served_again("a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a1", "c1", TURN_2)
    assert "restore" not in engine.events
    assert lines(caplog, "KV snapshot of agent a1 conversation c1 not restored at a switch from agent - "
                         "conversation - to an agent call (agent a1 conversation c1): the host cache "
                         "(cache_ram not 0) may hold a newer copy: this launch served the conversation "
                         "after the call its snapshot was saved from")
    # Kept; c2 was never saved, since the direct calls moved the record past it with no switch.
    assert [e["conversation_id"] for e in entries(folder)] == ["c1"]


@pytest.mark.asyncio
async def test_under_a_host_cache_an_older_entry_without_its_call_falls_back_to_the_narrow_gate(tmp_path, caplog):
    """An index entry written before the field: served by this launch, so not restored, as before."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    await turn(p, "a2", "c2")
    index = kv.read_index(folder)
    for e in index["entries"]:
        e.pop("sent", None)
        e.pop("sent_mono", None)
    kv.write_index(folder, index)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a1", "c1", TURN_2)
    assert "restore" not in engine.events
    assert lines(caplog, "the host cache (cache_ram not 0) may hold a newer copy: this launch served the "
                         "conversation and the snapshot does not name the call it was saved from")


@pytest.mark.asyncio
async def test_with_no_host_cache_a_returning_conversation_is_restored(tmp_path):
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=0)
    await turn(p, "a1", "c1")
    await p.generate_response("summarise the day")  # A -> background saves A
    await turn(p, "a1", "c1", TURN_2)              # background -> A restores A
    assert engine.events == ["request:c1", "save", "save-end", "request:background", "restore", "request:c1"]


@pytest.mark.asyncio
async def test_a_400_restore_at_a_switch_keeps_the_file(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    engine.restore_answer = (400, {"error": {"message": "No available space in KV cache or invalid slot save file"}})
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert "restore" in engine.events
    assert final in [e["file"] for e in entries(folder)] and (folder / final).exists()
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.NO_ROOM


@pytest.mark.asyncio
async def test_two_occupied_slots_skip_the_save_and_the_restore_with_a_reason(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    engine.slot_list[1]["n_prompt_tokens"] = 5000
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert "save" not in engine.events and "restore" not in engine.events
    assert lines(caplog, "live conversation not saved at a switch from agent a1 conversation c1 to an agent "
                         "call (agent a2 conversation c2): 2 slots are occupied")
    assert lines(caplog, "KV snapshot of agent a2 conversation c2 not restored at a switch")
    assert [e["file"] for e in entries(folder)] == [final]


@pytest.mark.asyncio
async def test_a_second_incoming_call_after_a_switch_restore_neither_saves_nor_restores(tmp_path, caplog):
    """B restored into slot 0 but not yet sent (a retry, or a call on another loop): the record
    still names A, and C must not save B's restored slot under A's key."""
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=0)
    snapshot_of(p, folder, "a2", "c2")
    snapshot_of(p, folder, "a3", "c3")
    await turn(p, "a1", "c1")
    wire_b = p._anthropic_to_openai_messages(SYSTEM, TURN_2, provider=p)
    await sup.kv_at_a_switch(caller_kind="agent", agent_id="a2", conversation_id="c2",
                             peer_id=None, messages=wire_b)
    assert engine.events[-3:] == ["save", "save-end", "restore"]
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.kv_at_a_switch(caller_kind="agent", agent_id="a3", conversation_id="c3",
                                 peer_id=None, messages=wire_b)
    assert engine.events.count("save") == 1 and engine.events.count("restore") == 1
    assert lines(caplog, "slot 0 was restored at a switch after the last sent call, so it no longer holds that call")
    assert [e["conversation_id"] for e in entries(folder) if e["conversation_id"] == "c1"] == ["c1"]


@pytest.mark.asyncio
async def test_a_slot_the_index_already_holds_is_not_saved_again(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    # Two background calls seen at a switch from A, neither sent (cancelled before its slot).
    await sup.kv_at_a_switch(caller_kind=None, agent_id=None, conversation_id=None, peer_id=None, messages=None)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.kv_at_a_switch(caller_kind=None, agent_id=None, conversation_id=None, peer_id=None,
                                 messages=None)
    assert engine.events.count("save") == 1
    assert lines(caplog, "live conversation not saved at a switch from agent a1 conversation c1 to a "
                         "background call: already saved: the index holds this slot (2000 tokens")


@pytest.mark.asyncio
async def test_a_strata_supervisor_is_handed_no_switch(tmp_path):
    """Guard of unchanged behaviour: Strata is a later slice; its provider hook hands nothing over."""
    from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import (
        FakeSupervisor,
    )

    class StrataLike(FakeSupervisor):
        ENGINE = "strata"

        def __init__(self):
            super().__init__(ALIAS)
            self.switches = []

        async def kv_at_a_switch(self, **kw):
            self.switches.append(kw)

    folder = tmp_path / "kv"
    folder.mkdir()
    kv.register_folder(ALIAS, folder, cap_bytes=kv.GIB)
    p = llama(ALIAS)
    sup, _ = wire(p, sup=StrataLike())
    await turn(p, "a1", "c1")
    await turn(p, "a2", "c2")
    assert sup.switches == []


# --- review round: the restored slot, calls slipping in during the checks, unsaved slot 0 ---------


def direct_call(kind, agent=None, conversation=None, peer=None, prefix=("ef" * 32, 3)):
    """A call noted and marked sent with no provider, as one that bypasses the hook would be."""
    if conversation:
        kv.note_clearable(conversation)
    seq = kv.note_engine_call(ALIAS, caller_kind=kind, agent_id=agent, conversation_id=conversation,
                              peer_id=peer, prefix=prefix if kind in ("agent", "owner") else None, at=time.time())
    assert kv.mark_sent(ALIAS, seq)


async def switch_to(sup, p, agent, conversation, messages=TURN_2):
    """The hook of an incoming owner call that never reaches `mark_sent` afterwards."""
    await sup.kv_at_a_switch(caller_kind="agent", agent_id=agent, conversation_id=conversation, peer_id=None,
                             messages=p._anthropic_to_openai_messages(SYSTEM, messages, provider=p))


@pytest.mark.asyncio
async def test_a_drain_after_a_switch_restore_never_saves_the_restored_slot_under_the_outgoing_key(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    await switch_to(sup, p, "a2", "c2")  # A saved, B restored into slot 0, B never sent
    a_before, = [e for e in entries(folder) if e["conversation_id"] == "c1"]
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.drain(timeout=None)
    assert engine.events.count("save") == 1
    a_after, = [e for e in entries(folder) if e["conversation_id"] == "c1"]
    assert (a_after["file"], a_after["n_saved"]) == (a_before["file"], 2000)
    assert lines(caplog, "live conversation not saved: slot 0 was restored at a switch after the last sent call")


@pytest.mark.asyncio
async def test_the_outgoing_conversation_returning_before_the_incoming_is_sent_is_restored(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    await switch_to(sup, p, "a2", "c2")
    await turn(p, "a1", "c1", TURN_2)  # A again: slot 0 holds B, so A comes back from its file
    assert engine.events[-3:] == ["restore", "restore", "request:c1"]
    assert sup._kv_restored_after_sent is None  # slot 0 holds the record's call again


@pytest.mark.asyncio
async def test_a_call_sent_during_the_switch_saves_checks_stops_the_save(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")

    async def peer_slips_in(tokens):
        sup._in_flight += 1  # a peer's call takes its slot and is sent while the save reads the ledger
        direct_call("peer", peer="dpc-node-peer")
        engine.slot_list[0]["n_prompt_tokens"] = 9999
        return None, 0

    sup._kv_cold_rate = peer_slips_in
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.kv_at_a_switch(caller_kind=None, agent_id=None, conversation_id=None, peer_id=None,
                                 messages=None)
    sup._in_flight -= 1
    assert "save" not in engine.events and entries(folder) == []
    assert lines(caplog, "live conversation not saved at a switch from agent a1 conversation c1 to a "
                         "background call: another call reached the engine during the save's checks")


@pytest.mark.asyncio
async def test_a_call_sent_during_the_stop_saves_checks_stops_the_save(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")

    async def call_slips_in(tokens):
        direct_call(None)
        return None, 0

    sup._kv_cold_rate = call_slips_in
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.drain(timeout=None)
    assert "save" not in engine.events and entries(folder) == []
    assert lines(caplog, "live conversation not saved: another call was sent during the save's checks")


@pytest.mark.asyncio
async def test_an_unsaved_outgoing_conversation_in_slot_0_is_not_replaced_by_a_restore(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    direct_call("owner", conversation="chat-1")  # the owner's chat: never saved in slice 1
    engine.slot_list[0]["n_prompt_tokens"] = 50000
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert engine.events == ["request:c2"]
    assert lines(caplog, "KV snapshot of agent a2 conversation c2 not restored at a switch from agent - "
                         "conversation chat-1 to an agent call (agent a2 conversation c2): slot 0 holds the "
                         "outgoing call's 50000 tokens, which no snapshot holds; " + final + " kept")
    assert [e["file"] for e in entries(folder)] == [final]


@pytest.mark.asyncio
async def test_a_peer_at_a_switch_runs_the_owners_save_and_writes_no_second_file(tmp_path):
    """The slot queue, point 8: a peer's switch runs the save as an owner's does; a slot the index
    already holds is skipped as already saved, not written again."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    await sup.kv_at_a_switch(caller_kind=None, agent_id=None, conversation_id=None, peer_id=None, messages=None)
    assert kv.status_of(ALIAS)["last_save"]["outcome"] == kv.SAVED
    await sup.kv_at_a_switch(caller_kind="peer", agent_id=None, conversation_id=None, peer_id="dpc-node-peer",
                             messages=None)
    assert kv.status_of(ALIAS)["last_save"]["reason"].startswith("already saved")
    assert len(entries(folder)) == 1 and engine.events.count("save") == 1


@pytest.mark.asyncio
async def test_after_a_failed_switch_restore_the_next_switch_says_slot_0_was_wiped(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path, cache_ram_mib=0)
    snapshot_of(p, folder, "a2", "c2")
    snapshot_of(p, folder, "a3", "c3")
    engine.restore_answer = (400, {"error": {"message": "No available space in KV cache or invalid slot save file"}})
    await turn(p, "a1", "c1")
    await switch_to(sup, p, "a2", "c2")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await switch_to(sup, p, "a3", "c3")
    assert lines(caplog, "a restore at a switch after the last sent call failed and wiped slot 0")
    assert not lines(caplog, "slot 0 was restored at a switch")


# --- second review round -------------------------------------------------------------------------


def during(sup, action, hook):
    """Run `hook()` while the engine handles `action` (the request is on its way)."""
    real = sup._slot_action

    async def slot_action(slot, what, filename, timeout):
        if what == action:
            hook()
        return await real(slot, what, filename, timeout)

    sup._slot_action = slot_action


@pytest.mark.asyncio
async def test_a_peer_sent_while_the_switch_save_is_on_its_way_leaves_no_entry(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")

    def peer_first():
        direct_call("peer", peer="dpc-node-peer")
        engine.slot_list[0]["n_prompt_tokens"] = 9999

    during(sup, "save", peer_first)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.kv_at_a_switch(caller_kind=None, agent_id=None, conversation_id=None, peer_id=None,
                                 messages=None)
    assert entries(folder) == [] and not list(folder.glob("dpc-kv-*"))
    assert lines(caplog, "another call was sent during the save; the file is deleted")


@pytest.mark.asyncio
async def test_a_stop_save_whose_slot_changed_on_its_way_leaves_no_entry(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    during(sup, "save", lambda: engine.slot_list[0].update(n_prompt_tokens=9999))
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.drain(timeout=None)
    assert entries(folder) == [] and not list(folder.glob("dpc-kv-*"))
    assert lines(caplog, "the engine saved 9999 tokens where the checks measured 2000; the file is deleted")


@pytest.mark.asyncio
async def test_an_owner_call_sent_during_a_switch_restore_is_not_saved_by_the_next_drain(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    during(sup, "restore", lambda: direct_call("agent", agent="a9", conversation="c9"))
    await switch_to(sup, p, "a2", "c2")
    await sup.drain(timeout=None)
    assert [e["conversation_id"] for e in entries(folder) if e["conversation_id"] == "c9"] == []
    assert engine.events.count("save") == 1


@pytest.mark.asyncio
async def test_a_relaunch_forgets_what_a_switch_restore_left_in_slot_0(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    await switch_to(sup, p, "a2", "c2")
    assert sup._kv_restored_after_sent is not None
    await sup.stop()
    assert sup._kv_restored_after_sent is None
    sup._kv_restored_after_sent = (1, "left over")
    await sup.ensure_running()
    assert sup._kv_restored_after_sent is None


@pytest.mark.asyncio
async def test_an_unsaved_outgoing_conversation_in_slot_1_blocks_the_restore_into_slot_0(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    direct_call("owner", conversation="chat-1")
    engine.slot_list[1]["n_prompt_tokens"] = 50000
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert "restore" not in engine.events
    assert lines(caplog, "slot 1 holds the outgoing call's 50000 tokens, which no snapshot holds; " + final + " kept")


def measured(folder, write=1e8, read=9e7):
    kv.write_index(folder, {**kv.read_index(folder), "rates": {"write_bytes_per_s": write, "read_bytes_per_s": read}})


@pytest.mark.asyncio
async def test_a_restore_slower_than_its_cold_read_is_skipped_at_a_switch(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    measured(folder, write=1.0, read=1.0)  # 1024 bytes take ~1024 s

    async def fast_cold(tokens):
        return 1000.0, 5

    sup._kv_cold_rate = fast_cold
    direct_call(None)  # a background call holds slot 0
    engine.slot_list[0]["n_prompt_tokens"] = 300
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert "restore" not in engine.events
    assert lines(caplog, "not restored at a switch from agent - conversation - to an agent call (agent a2 "
                         "conversation c2): below the size threshold: restore ~1024.0 s of 777 tokens")
    assert [e["file"] for e in entries(folder)] == [final]


@pytest.mark.asyncio
async def test_a_switch_that_only_restores_prints_one_decision_line(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    measured(folder)
    direct_call(None)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    decision, = lines(caplog, "KV snapshot decision")
    assert decision.endswith("KV snapshot decision at a switch from agent - conversation - to an agent call "
                             "(agent a2 conversation c2): no save (the last sent call was no other owner "
                             "conversation's); incoming restore ~0.0 s of 777 tokens (read rate 90 MB/s), "
                             "threshold unknown")
    assert "restore" in engine.events


@pytest.mark.asyncio
async def test_the_switch_decision_line_names_the_incoming_restores_wait(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    measured(folder)
    await turn(p, "a1", "c1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    decision, = lines(caplog, "KV snapshot decision")
    assert "saving (" in decision
    assert decision.endswith("; incoming restore ~0.0 s of 777 tokens (read rate 90 MB/s), threshold unknown")


@pytest.mark.asyncio
async def test_a_stop_save_of_a_call_sent_to_an_earlier_launch_is_skipped(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    sup._kv_launched_mono = time.monotonic() + 1  # this launch began after A was sent
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.drain(timeout=None)
    assert "save" not in engine.events
    assert lines(caplog, "live conversation not saved: the last call was sent to an earlier launch of the engine")


@pytest.mark.asyncio
async def test_slot_0_after_a_relaunch_is_named_as_no_call_of_this_launch(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    await turn(p, "a1", "c1")
    sup._kv_launched_mono = time.monotonic() + 1
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", TURN_2)
    assert "restore" not in engine.events
    assert lines(caplog, "slot 0 holds 2000 tokens that no call of this launch names; " + final + " kept")


# --- P5, the real engine's answers (b12110, -np 2 --kv-unified) -----------------------------------

EMPTIED_SLOT_SAVE = {"id_slot": 0, "n_saved": 0, "n_written": 21720}
TRUNCATED_FILE_BYTES = 891718378
TRUNCATED_RESTORE = {"id_slot": 0, "filename": "p5a-cram8192-slot0-trunc.bin", "n_restored": 30338,
                     "n_read": 716934272, "timings": {"restore_ms": 474.189}}


@pytest.mark.asyncio
async def test_a_save_of_a_slot_the_host_cache_emptied_leaves_no_entry(tmp_path, caplog):
    """Guard: the save's count check already discards this answer."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    real = sup._slot_action

    async def emptied(slot, what, filename, timeout):
        if what != "save":
            return await real(slot, what, filename, timeout)
        (folder / filename).write_bytes(b"\0" * 21720)
        return 200, dict(EMPTIED_SLOT_SAVE)

    sup._slot_action = emptied
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await sup.kv_at_a_switch(caller_kind=None, agent_id=None, conversation_id=None, peer_id=None,
                                 messages=None)
    assert entries(folder) == [] and not list(folder.glob("dpc-kv-*"))
    assert lines(caplog, "the engine saved 0 tokens where the checks measured 2000; the file is deleted")


def _gone(folder, final):
    try:
        (folder / final).unlink()
    except OSError:
        pass


@pytest.mark.asyncio
async def test_an_empty_engine_restore_that_read_less_than_the_file_deletes_it(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a1", "c1", size=TRUNCATED_FILE_BYTES)
    try:
        engine.restore_answer = (200, dict(TRUNCATED_RESTORE))
        sup._kv_engine_fresh = True
        with caplog.at_level(logging.INFO, logger=SUP_LOG):
            await turn(p, "a1", "c1", TURN_2)
        assert "restore" in engine.events
        assert entries(folder) == [] and not (folder / final).exists()
        last = kv.status_of(ALIAS)["last_restore"]
        assert last["outcome"] == kv.FAILED
        assert "the engine read 716934272 of the file's 891718378 bytes" in last["reason"]
    finally:
        _gone(folder, final)


@pytest.mark.asyncio
async def test_a_switch_restore_that_read_less_than_the_file_deletes_it_and_claims_nothing(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2", size=TRUNCATED_FILE_BYTES)
    try:
        engine.restore_answer = (200, dict(TRUNCATED_RESTORE))
        await turn(p, "a1", "c1")
        await switch_to(sup, p, "a2", "c2")
        assert "restore" in engine.events
        assert final not in [e["file"] for e in entries(folder)] and not (folder / final).exists()
        assert sup._kv_restored_after_sent[1] != base_mod._KV_SLOT_RESTORED
        assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.FAILED
    finally:
        _gone(folder, final)


# --- the incoming call's deadline (Johnny's review of e7201c0d, point 1) ---------------------------


async def turn_by(p, agent, conversation, messages, wait_s):
    """A turn whose hold was accepted now with `wait_s` seconds of wait: its one deadline."""
    from types import SimpleNamespace

    kv.note_clearable(conversation)
    hold = SimpleNamespace(alias=ALIAS, live=True, agent_id=agent, conversation_id=conversation, borrow=None,
                           caller=None, accepted_at=base_mod._swap_clock(), wait_s=wait_s)
    with carrying_card_hold(hold):
        return await p.generate_with_tools(messages, [], system=SYSTEM)


def restore_timeouts(sup):
    seen = []
    real = sup._slot_action

    async def slot_action(slot, what, filename, timeout):
        if what == "restore":
            seen.append(timeout)
        return await real(slot, what, filename, timeout)

    sup._slot_action = slot_action
    return seen


@pytest.mark.asyncio
async def test_a_switch_save_longer_than_the_incoming_calls_deadline_is_skipped(tmp_path, caplog, monkeypatch):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1")
    monkeypatch.setattr(kv, "estimate_save_s", lambda nbytes, rates: 30.0)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn_by(p, "a2", "c2", TURN_1, wait_s=10)
    assert "save" not in engine.events and entries(folder) == []
    skip, = lines(caplog, "live conversation not saved at a switch")
    assert "estimate over the bound: ~30.0 s against" in skip
    left = float(skip.split("against ")[1].split(" s left")[0])
    assert 0 < left <= 10


@pytest.mark.asyncio
async def test_a_switch_restore_waits_no_longer_than_the_incoming_calls_deadline(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    snapshot_of(p, folder, "a2", "c2")
    measured(folder)
    direct_call(None)
    seen = restore_timeouts(sup)
    await turn_by(p, "a2", "c2", TURN_2, wait_s=10)
    assert len(seen) == 1 and 0 < seen[0] <= 10


@pytest.mark.asyncio
async def test_a_switch_restore_longer_than_the_time_left_is_skipped(tmp_path, caplog):
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2")
    measured(folder, write=1.0, read=1.0)  # 1024 bytes take ~1024 s
    direct_call(None)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn_by(p, "a2", "c2", TURN_2, wait_s=10)
    assert "restore" not in engine.events
    assert lines(caplog, "not restored at a switch from agent - conversation - to an agent call (agent a2 "
                         "conversation c2): restore ~1024.0 s against")
    assert lines(caplog, "s left before the incoming request's deadline; " + final + " kept")


# --- a group chat: two of our agents in one conversation (Johnny's point 9) ------------------------


@pytest.mark.asyncio
async def test_in_a_group_one_agent_after_another_saves_the_first_and_finds_nothing_for_the_second(tmp_path, caplog):
    """Pins today's behaviour: the key is (agent, conversation), so Ark -> Johnny is a switch."""
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "ark", "group-1")
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "johnny", "group-1", TURN_2)
    assert engine.events == ["request:group-1", "save", "save-end", "request:group-1"]
    assert [(e["agent_id"], e["conversation_id"]) for e in entries(folder)] == [("ark", "group-1")]
    decision, = lines(caplog, "KV snapshot decision")
    assert ("at a switch from agent ark conversation group-1 to an agent call "
            "(agent johnny conversation group-1)") in decision
    assert decision.endswith("; incoming: no snapshot of this conversation in the index")
    assert lines(caplog, "KV snapshot of agent johnny conversation group-1 not restored at a switch")
