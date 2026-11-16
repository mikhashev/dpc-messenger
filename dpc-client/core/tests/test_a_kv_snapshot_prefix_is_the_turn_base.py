"""A KV snapshot's prefix is the turn base: the messages up to and including the last `user`
message (ADR-044, amendment of 2026-11-16).

An agent's turn sends system, history, one `user` trigger, then its tool rounds; the next turn
replays the history byte for byte but never the old tool rounds. Measured 2026-11-16: a snapshot
of 53 messages (base end 47) and, after a restart, a turn of 49 that shared messages 0..46. A
whole-list prefix could not match it; the turn base does. The data below mirrors that case.

The prefix is taken where the provider records the call (`_ensure_for`), so the test reads what
production writes. Fakes only: no engine, no network.
"""

import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers.llama_server_supervisor import card_held_for, stop_by_hand

from tests.test_a_kv_snapshot_is_restored_only_into_an_empty_engine_for_its_own_conversation import (
    RestoringSupervisor, clean,  # noqa: F401
)
from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    card,
    kv_state,
    note_call,
    saving_pair,
    settings,
    swap_on,
)
from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import (
    ALIAS, llama, wire,
)

SYSTEM = {"role": "system", "content": "you are Johnny"}
# Indices 1..45; index 45 is an assistant reply, so the trigger at 46 opens the turn.
HISTORY = [{"role": "assistant" if i % 2 == 0 else "user", "content": f"history {i}"} for i in range(45)]
TRIGGER_1 = {"role": "user", "content": "check the build"}
TOOL_ROUNDS = []
for n in range(3):
    TOOL_ROUNDS += [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": f"t{n}", "type": "function", "function": {"name": "run", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": f"t{n}", "content": f"result {n}"},
    ]
ANSWER_1 = {"role": "assistant", "content": "the build is green"}
TRIGGER_2 = {"role": "user", "content": "now the docs"}

SAVED = [SYSTEM] + HISTORY + [TRIGGER_1] + TOOL_ROUNDS  # 53, base end 47
NEW = [SYSTEM] + HISTORY + [TRIGGER_1, ANSWER_1, TRIGGER_2]  # 49, shares 0..46


def test_the_data_mirrors_the_live_case():
    assert (len(SAVED), len(NEW)) == (53, 49)
    assert kv.message_fingerprints(SAVED)["base_end"] == 47
    assert SAVED[:47] == NEW[:47] and SAVED[47] != NEW[47]
    assert NEW[46] == TRIGGER_1 and NEW[-1] == TRIGGER_2


async def _recorded(p, messages, **hold):
    """The record `_ensure_for` notes for this call, read before the slot marks it sent."""
    hold = hold or {"agent_id": "agent_7", "conversation_id": "conv-a"}
    async with card_held_for(ALIAS, "accepted_request", **hold):
        await p._ensure_for(messages)
    return dict(next(reversed(kv._PENDING[ALIAS].values())))


@pytest.mark.asyncio
async def test_the_record_holds_the_turn_base_not_the_whole_request():
    p = llama()
    wire(p)
    record = await _recorded(p, SAVED)
    assert (record["prefix_sha256"], record["prefix_messages"]) == kv.prefix_digest(SAVED, 47)


@pytest.mark.asyncio
async def test_a_turn_saved_after_tool_rounds_matches_the_next_turn():
    p = llama()
    wire(p)
    assert kv.matches(await _recorded(p, SAVED), NEW)


@pytest.mark.asyncio
async def test_a_changed_system_message_does_not_match():
    p = llama()
    wire(p)
    record = await _recorded(p, SAVED)
    assert not kv.matches(record, [{"role": "system", "content": "you are Johnny, today"}] + NEW[1:])


@pytest.mark.asyncio
async def test_a_history_edit_inside_the_base_does_not_match():
    p = llama()
    wire(p)
    record = await _recorded(p, SAVED)
    edited = list(NEW)
    edited[10] = {**edited[10], "content": "rewritten by a cut"}
    assert not kv.matches(record, edited)


@pytest.mark.asyncio
async def test_a_request_with_no_user_message_keeps_the_whole_list():
    p = llama()
    wire(p)
    no_user = [SYSTEM, ANSWER_1]
    record = await _recorded(p, no_user)
    assert (record["prefix_sha256"], record["prefix_messages"]) == kv.prefix_digest(no_user)


@pytest.mark.asyncio
async def test_after_a_restart_the_next_turn_restores_the_snapshot(tmp_path):
    """The live failure end to end: saved at 53, the next turn of 49 restores instead of deleting."""
    folder = tmp_path / "kv"
    folder.mkdir()
    kv.register_folder(ALIAS, folder, cap_bytes=kv.GIB)
    p = llama()
    sup, _ = wire(p, sup=RestoringSupervisor(folder))
    record = await _recorded(p, SAVED)
    staging, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"\0" * 1024)
    kv.release_staging(folder, staging)
    entry = kv.new_entry(file=final, alias=ALIAS, engine="llamacpp_server", identity={}, record=record,
                         n_saved=10, nbytes=1024, save_ms=3, now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=kv.GIB)
    sup._kv_engine_fresh = True

    async with card_held_for(ALIAS, "accepted_request", agent_id="agent_7", conversation_id="conv-a"):
        await p._ensure_for(NEW)

    assert [r["file"] for r in sup.restores] == [final]
    assert [e["file"] for e in kv.read_index(folder)["entries"]] == [final]


async def _prefix_the_provider_records(messages, **hold):
    p = llama()
    wire(p)
    record = await _recorded(p, messages, **hold)
    kv.reset_state()  # only the prefix is carried over to the saving supervisor
    return record["prefix_sha256"], record["prefix_messages"]


@pytest.mark.asyncio
async def test_the_owner_chats_one_message_prompt_is_still_not_saved(tmp_path):
    prefix = await _prefix_the_provider_records([{"role": "user", "content": "rebuilt prompt"}],
                                                conversation_id="chat-1")
    assert prefix[1] == 1
    holder, _new, fake, _folder = await saving_pair(tmp_path)
    note_call(kind=kv.OWNER_CHAT_KIND, agent=None, conversation="chat-1", prefix=prefix)

    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] != kv.SAVED and "single-prompt call: no stable prefix" in outcome["reason"]
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "go"}],
    [{"role": "user", "content": "go"}] + TOOL_ROUNDS,
], ids=["one message", "one-message base after tool rounds"])
async def test_an_agents_turn_whose_base_is_one_message_is_still_saved(tmp_path, messages):
    prefix = await _prefix_the_provider_records(messages)
    assert prefix[1] == 1
    holder, _new, _fake, folder = await saving_pair(tmp_path)
    note_call(prefix=prefix)

    assert (await stop_by_hand(holder))["outcome"] == kv.SAVED
    assert [e["prefix_messages"] for e in kv.read_index(folder)["entries"]] == [1]
