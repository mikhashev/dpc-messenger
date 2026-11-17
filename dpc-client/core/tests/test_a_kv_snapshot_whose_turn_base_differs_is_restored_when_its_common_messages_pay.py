"""A KV snapshot whose turn base differs is restored when its common messages pay (ADR-044,
amendment of 2026-11-17; Mike's call, 2026-11-17, DPC Project chat).

On llama-server only. When the request's turn base no longer hashes to the saved one, the index
entry's per-message short hashes (`prefix_hashes`) and cumulative character counts (`prefix_chars`)
of the saved turn base say how many leading whole messages the request still shares with it. The
reusable tokens are estimated as `n_saved` times the share of the saved call's characters
(`call_chars`, the tool rounds after the base included) those messages hold; the snapshot is restored when the estimated cold read of that common part takes longer
than the estimated restore of the file, and deleted otherwise, as before. No message in common, an
entry written before the fields, and Strata keep today's rule: an exact match or a deletion. The index
carries hashes and counts, never message text. One decision line on both paths, the empty engine and
a switch.

A real supervisor over the fake child of the card tests, as the switch tests drive it.
"""

import json
import logging
import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import carrying_card_hold

from tests.test_a_kv_snapshot_is_restored_only_into_an_empty_engine_for_its_own_conversation import (
    RestoringSupervisor,
)
from tests.test_a_kv_snapshot_is_saved_and_restored_at_an_agent_switch import (  # noqa: F401
    ALIAS, direct_call, entries, kv_state, lines, measured, snapshot_of, switching,
)
from tests.test_one_local_engine_holds_the_card import _strata, card  # noqa: F401
from tests.test_the_last_engine_call_names_owner_peer_or_gateway_from_the_calls_own_context import (
    llama, owner_hold, wire,
)

SUP_LOG = base_mod.logger.name
PROVIDER_LOG = "dpc_client_core.providers.llamacpp_server_provider"
SYSTEM = "you are a helper"
N_SAVED = 50_000
# The saved turn: system, a long user message, a long reply, the trigger (turn base of 4).
SAVED = [{"role": "user", "content": "list the files " + "f" * 2000},
         {"role": "assistant", "content": "a.txt b.txt " + "r" * 2000},
         {"role": "user", "content": "open a.txt"}]
# The same first two wire messages (system, the long user message), then a reply that changed.
PARTLY = [SAVED[0], {"role": "assistant", "content": "a.txt b.txt, rewritten by a context cut"},
          {"role": "user", "content": "open b.txt"}]
EXACT = SAVED + [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "thanks"}]
# The saved call: the turn base, then a long round after the trigger, which the slot holds too.
SAVED_CALL = SAVED + [{"role": "assistant", "content": "reading a.txt " + "w" * 3000}]
ESTIMATE = "estimate; the engine rounds down to a checkpoint, up to ~8192 tokens lower)"


def _chars(message):
    return len(json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str))


def _wire(p, messages, system=SYSTEM):
    return p._anthropic_to_openai_messages(system, messages, provider=p)


def expected_tokens(p, common):
    """`n_saved` times the share of the saved call's characters the first `common` messages hold."""
    sizes = [_chars(m) for m in _wire(p, SAVED_CALL)]
    return int(N_SAVED * sum(sizes[:common]) / sum(sizes))


def snapshot_with_hashes(p, folder, agent, conversation, messages=SAVED_CALL, size=1024, system=SYSTEM):
    """An indexed snapshot saved from a call whose record carries the call's fingerprint, as the
    provider notes it (`_note_engine_call`)."""
    wire_messages = _wire(p, messages, system)
    sha, k = kv.prefix_digest(wire_messages, kv.base_end(wire_messages))
    staging, final = kv.new_snapshot_names(folder)
    with open(folder / final, "wb") as f:
        f.truncate(size)
    kv.release_staging(folder, staging)
    record = {"agent_id": agent, "conversation_id": conversation, "caller_kind": "agent",
              "prefix_sha256": sha, "prefix_messages": k, "fingerprint": kv.message_fingerprints(wire_messages)}
    entry = kv.new_entry(file=final, alias=ALIAS, engine="llamacpp_server", identity={}, record=record,
                         n_saved=N_SAVED, nbytes=size, save_ms=3, now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=kv.GIB)
    return final


async def turn(p, agent, conversation, messages, system=SYSTEM):
    kv.note_clearable(conversation)
    with carrying_card_hold(owner_hold(ALIAS, agent, conversation)):
        return await p.generate_with_tools(messages, [], system=system)


def cold_at(sup, rate=1000.0):
    async def cold(tokens):
        return rate, 5

    sup._kv_cold_rate = cold


async def at_a_switch(tmp_path, **cfg):
    """A background call holds slot 0; the incoming owner call of c2 is the switch."""
    p, sup, engine, folder = await switching(tmp_path, **cfg)
    direct_call(None)
    engine.slot_list[0]["n_prompt_tokens"] = 300
    return p, sup, engine, folder


async def empty_engine(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    sup._kv_engine_fresh = True
    return p, sup, engine, folder


# --- the index ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_index_carries_the_turn_bases_short_hashes_and_character_counts_never_its_text(tmp_path):
    p, sup, engine, folder = await switching(tmp_path)
    await turn(p, "a1", "c1", SAVED)
    await turn(p, "a2", "c2", SAVED)  # the switch saves c1
    entry, = entries(folder)
    wire_messages = _wire(p, SAVED)
    assert entry["prefix_messages"] == 4
    assert entry["prefix_hashes"] == kv.message_fingerprints(wire_messages)["hashes"][:4]
    sizes = [_chars(m) for m in wire_messages]
    assert entry["prefix_chars"] == [sum(sizes[:i + 1]) for i in range(4)]
    assert entry["call_chars"] == sum(sizes)
    text = (folder / kv.INDEX_NAME).read_text(encoding="utf-8")
    for m in wire_messages:
        assert m["content"] not in text
    assert "fingerprint" not in entry and "base_end" not in entry and "msg0_chars" not in entry


def test_the_common_part_counts_leading_whole_messages_and_estimates_by_the_calls_characters():
    msgs = [{"role": "system", "content": "s" * 100}, {"role": "user", "content": "u" * 300},
            {"role": "assistant", "content": "a" * 100}, {"role": "user", "content": "t"},
            {"role": "assistant", "content": "a tool round after the trigger " + "r" * 500}]
    sha, k = kv.prefix_digest(msgs, kv.base_end(msgs))
    record = {"prefix_sha256": sha, "prefix_messages": k, "fingerprint": kv.message_fingerprints(msgs)}
    entry = kv.new_entry(file="dpc-kv-ab.bin", alias=ALIAS, engine="llamacpp_server", identity={},
                         record=record, n_saved=1000, nbytes=1, save_ms=1, now=time.time())
    sizes = [_chars(m) for m in msgs]
    assert entry["call_chars"] == sum(sizes)
    changed = msgs[:2] + [{"role": "assistant", "content": "other"}, msgs[3]]
    part = kv.common_part(entry, changed)
    assert (part["messages"], part["of"], part["n_saved"]) == (2, 4, 1000)
    # The share is of the whole saved call, the round after the trigger included.
    assert part["tokens"] == int(1000 * sum(sizes[:2]) / sum(sizes))
    # Without the call's total, or with one shorter than the base, the base's characters stand in.
    for without in ({k: v for k, v in entry.items() if k != "call_chars"}, {**entry, "call_chars": 5}):
        assert kv.common_part(without, changed)["tokens"] == int(1000 * sum(sizes[:2]) / sum(sizes[:4]))
    # A request shorter than the base shares at most its own messages.
    assert kv.common_part(entry, msgs[:1])["messages"] == 1
    assert kv.common_part(entry, [{"role": "system", "content": "x"}] + msgs[1:])["messages"] == 0
    # An entry written before the fields, or with fields that do not fit, has no common part.
    old = {k: v for k, v in entry.items() if k not in ("prefix_hashes", "prefix_chars", "call_chars")}
    assert kv.common_part(old, changed) is None
    assert kv.common_part({**entry, "prefix_chars": entry["prefix_chars"][:2]}, changed) is None
    assert kv.common_part({**entry, "prefix_hashes": "abc"}, changed) is None


# --- at a switch ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_a_switch_a_partly_matching_snapshot_whose_common_part_pays_is_restored(tmp_path, caplog):
    p, sup, engine, folder = await at_a_switch(tmp_path)
    final = snapshot_with_hashes(p, folder, "a2", "c2")
    measured(folder)  # read 90 MB/s: 1024 bytes in ~0 s
    cold_at(sup)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", PARTLY)
    assert engine.events == ["restore", "request:c2"]
    assert final in [e["file"] for e in entries(folder)]
    tokens = expected_tokens(p, 2)
    line, = lines(caplog, "partly matches")
    assert line.startswith(f"llama-server[{ALIAS}]: KV snapshot of agent a2 conversation c2 partly matches "
                           "at a switch from agent - conversation - to an agent call (agent a2 conversation c2): ")
    assert f"2 of 4 messages common (~{tokens} of {N_SAVED} tokens, {ESTIMATE}" in line
    assert f"against a cold read of the common part ~{tokens / 1000.0:.1f} s" in line
    assert line.endswith(": restoring")
    assert not lines(caplog, "the prefix moved")
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.RESTORED


@pytest.mark.asyncio
async def test_at_a_switch_a_partly_matching_snapshot_below_the_time_rule_is_deleted(tmp_path, caplog):
    p, sup, engine, folder = await at_a_switch(tmp_path)
    final = snapshot_with_hashes(p, folder, "a2", "c2")
    # ~40 s to restore: under the cold read of all 50,000 tokens (50 s), over that of the common part.
    measured(folder, write=25.6, read=25.6)
    cold_at(sup)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", PARTLY)
    assert engine.events == ["request:c2"]
    assert entries(folder) == [] and not (folder / final).exists()
    line, = lines(caplog, "partly matches")
    assert "2 of 4 messages common" in line and "restore ~40.0 s" in line
    assert line.endswith(f": {final} deleted, the call runs cold")
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == "prefix_mismatch"


@pytest.mark.asyncio
async def test_at_a_switch_a_divergence_in_the_first_message_deletes_as_before(tmp_path, caplog):
    p, sup, engine, folder = await at_a_switch(tmp_path)
    final = snapshot_with_hashes(p, folder, "a2", "c2")
    measured(folder)
    cold_at(sup)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", SAVED, system="you are a changed helper")
    assert engine.events == ["request:c2"]
    assert entries(folder) == [] and not (folder / final).exists()
    assert lines(caplog, f"the prefix moved since the save; {final} deleted, the call runs cold")
    assert not lines(caplog, "partly matches")


@pytest.mark.asyncio
async def test_at_a_switch_an_entry_without_per_message_hashes_deletes_as_before(tmp_path, caplog):
    p, sup, engine, folder = await at_a_switch(tmp_path)
    final = snapshot_of(p, folder, "a2", "c2", SAVED)  # no fingerprint on its record: an older entry
    measured(folder)
    cold_at(sup)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", PARTLY)
    assert engine.events == ["request:c2"]
    assert entries(folder) == []
    assert lines(caplog, f"the prefix moved since the save; {final} deleted, the call runs cold")
    assert not lines(caplog, "partly matches")


@pytest.mark.asyncio
async def test_at_a_switch_an_exact_match_restores_as_before_with_no_partial_line(tmp_path, caplog):
    p, sup, engine, folder = await at_a_switch(tmp_path)
    final = snapshot_with_hashes(p, folder, "a2", "c2")
    measured(folder)
    cold_at(sup)
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", EXACT)
    assert engine.events == ["restore", "request:c2"]
    assert final in [e["file"] for e in entries(folder)]
    assert not lines(caplog, "partly matches")


@pytest.mark.asyncio
async def test_at_a_switch_a_partial_restore_keeps_the_slot_held_check(tmp_path, caplog):
    """Guard: the common part pays, but slot 0 holds an owner call no snapshot holds."""
    p, sup, engine, folder = await switching(tmp_path)
    final = snapshot_with_hashes(p, folder, "a2", "c2")
    measured(folder)
    cold_at(sup)
    direct_call("owner", conversation="chat-1")
    engine.slot_list[0]["n_prompt_tokens"] = 50000
    with caplog.at_level(logging.INFO, logger=SUP_LOG):
        await turn(p, "a2", "c2", PARTLY)
    assert "restore" not in engine.events
    assert lines(caplog, "slot 0 holds the outgoing call's 50000 tokens, which no snapshot holds; " + final + " kept")
    assert [e["file"] for e in entries(folder)] == [final]


# --- into an empty engine -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_into_an_empty_engine_a_partly_matching_snapshot_whose_common_part_pays_is_restored(tmp_path, caplog):
    p, sup, engine, folder = await empty_engine(tmp_path)
    final = snapshot_with_hashes(p, folder, "a1", "c1")
    measured(folder)
    cold_at(sup)
    with caplog.at_level(logging.INFO):
        await turn(p, "a1", "c1", PARTLY)
    assert engine.events == ["restore", "request:c1"]
    assert final in [e["file"] for e in entries(folder)]
    tokens = expected_tokens(p, 2)
    line, = lines(caplog, "partly matches")
    assert line.startswith(f"{ALIAS}: KV snapshot {final} (agent=a1 conv=c1) partly matches: ")
    assert f"2 of 4 messages common (~{tokens} of {N_SAVED} tokens, {ESTIMATE}" in line
    assert line.endswith(": restoring")
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == kv.RESTORED


@pytest.mark.asyncio
async def test_into_an_empty_engine_a_partly_matching_snapshot_below_the_time_rule_is_deleted(tmp_path, caplog):
    p, sup, engine, folder = await empty_engine(tmp_path)
    final = snapshot_with_hashes(p, folder, "a1", "c1")
    measured(folder, write=1.0, read=1.0)  # 1024 bytes take ~1024 s
    cold_at(sup)
    with caplog.at_level(logging.INFO):
        await turn(p, "a1", "c1", PARTLY)
    assert engine.events == ["request:c1"]
    assert entries(folder) == [] and not (folder / final).exists()
    line, = lines(caplog, "partly matches")
    assert "2 of 4 messages common" in line and "restore ~1024.0 s" in line
    assert line.endswith(": deleted, the call runs cold")
    assert kv.status_of(ALIAS)["last_restore"]["outcome"] == "prefix_mismatch"


@pytest.mark.asyncio
async def test_into_an_empty_engine_a_divergence_in_the_first_message_deletes_as_before(tmp_path, caplog):
    p, sup, engine, folder = await empty_engine(tmp_path)
    final = snapshot_with_hashes(p, folder, "a1", "c1")
    measured(folder)
    cold_at(sup)
    with caplog.at_level(logging.INFO):
        await turn(p, "a1", "c1", SAVED, system="you are a changed helper")
    assert engine.events == ["request:c1"]
    assert entries(folder) == [] and not (folder / final).exists()
    assert lines(caplog, "not restored: the prefix moved since the save; deleted, the call runs cold")
    assert not lines(caplog, "partly matches")


@pytest.mark.asyncio
async def test_into_an_empty_engine_an_entry_without_per_message_hashes_deletes_as_before(tmp_path, caplog):
    p, sup, engine, folder = await empty_engine(tmp_path)
    snapshot_of(p, folder, "a1", "c1", SAVED)
    measured(folder)
    cold_at(sup)
    with caplog.at_level(logging.INFO):
        await turn(p, "a1", "c1", PARTLY)
    assert engine.events == ["request:c1"]
    assert entries(folder) == []
    assert lines(caplog, "not restored: the prefix moved since the save; deleted, the call runs cold")
    assert not lines(caplog, "partly matches")


@pytest.mark.asyncio
async def test_into_an_empty_engine_an_exact_match_restores_whatever_the_rates(tmp_path, caplog):
    """Guard of unchanged behaviour: the exact match has no time rule into an empty engine."""
    p, sup, engine, folder = await empty_engine(tmp_path)
    snapshot_with_hashes(p, folder, "a1", "c1")
    measured(folder, write=1.0, read=1.0)
    cold_at(sup)
    with caplog.at_level(logging.INFO):
        await turn(p, "a1", "c1", EXACT)
    assert engine.events == ["restore", "request:c1"]
    assert not lines(caplog, "partly matches")


# --- Strata keeps the exact match -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_strata_supervisor_has_no_common_part(tmp_path):
    sup = _strata(tmp_path)
    p = llama(ALIAS)
    folder = tmp_path / "kv"
    folder.mkdir()
    final = snapshot_with_hashes(p, folder, "a1", "c1")
    entry, = [e for e in entries(folder) if e["file"] == final]
    assert await sup._kv_common_part(folder, entry, _wire(p, PARTLY)) is None


@pytest.mark.asyncio
async def test_into_an_empty_strata_engine_a_partly_matching_snapshot_is_deleted_as_before(tmp_path, caplog):
    folder = tmp_path / "kv"
    folder.mkdir()
    kv.register_folder(ALIAS, folder, cap_bytes=kv.GIB)
    sup = RestoringSupervisor(folder, engine="strata")
    asked = []

    async def common_part(*args):
        asked.append(args)
        return True, "would restore"

    sup._kv_common_part = common_part
    p = llama(ALIAS)
    wire(p, sup=sup)
    final = snapshot_with_hashes(p, folder, "a1", "c1")
    sup._kv_engine_fresh = True
    with caplog.at_level(logging.INFO, logger=PROVIDER_LOG):
        await turn(p, "a1", "c1", PARTLY)
    assert sup.restores == [] and asked == []
    assert entries(folder) == [] and not (folder / final).exists()
    assert lines(caplog, "not restored: the prefix moved since the save; deleted, the call runs cold")
