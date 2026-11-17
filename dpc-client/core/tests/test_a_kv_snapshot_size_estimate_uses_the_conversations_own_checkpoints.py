"""A KV snapshot's size estimate adds this conversation's own context checkpoints.

The alias keeps the checkpoint bytes of its last measured save, often another conversation's:
on 2026-11-17 a conversation whose own save carried 4 checkpoints was estimated with the 11 of
the save before it, and the next one the other way round. Since the estimate only decides
whether a save fits its bound (amendment of 2026-11-17), it is the best guess for this one:
the checkpoint bytes its own entry recorded at its previous save; none, the median over the
alias's size samples; none there either, the alias's last value. A whole-file fit (no
checkpoint bytes held) adds nothing, as before, and an entry written before the field falls back.
"""

import logging
import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers import llama_server_supervisor as base_mod
from dpc_client_core.managers.llama_server_supervisor import stop_by_hand

from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    ALIAS, card, kv_state, note_call, saving_pair, settings, swap_on,
)

MIB = 1024 ** 2
CK = 150 * MIB                               # one checkpoint
FEW, MANY = 4 * CK, 11 * CK


def _save(folder, conversation, tokens, ck_bytes, *, ck_count=None, alias="qwen", identity=None):
    """One indexed save of `conversation` whose file holds `ck_bytes` of checkpoints."""
    _, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"x" * 8)
    record = {"agent_id": "a1", "conversation_id": conversation, "caller_kind": "agent",
              "prefix_sha256": "ab", "prefix_messages": 2}
    nbytes = int(0.47e9 + 20e3 * tokens + (ck_bytes or 0))
    entry = kv.new_entry(file=final, alias=alias, engine="llamacpp_server",
                         identity=identity or {"engine_version": "1"}, record=record,
                         n_saved=tokens, nbytes=nbytes, save_ms=20_000, now=time.time())
    assert kv.add_entry(folder, entry, cap_bytes=10 ** 13, checkpoint_count=ck_count,
                        checkpoint_bytes=ck_bytes)
    return final


def _ck(folder, conversation, alias="qwen"):
    sizes = kv.alias_sizes(folder, alias, "llamacpp_server", agent_id="a1", conversation_id=conversation)
    return sizes.get("checkpoint_bytes")


@pytest.mark.parametrize("first, second", [(("c1", FEW), ("c2", MANY)), (("c2", MANY), ("c1", FEW))])
def test_each_conversation_is_estimated_with_its_own_last_checkpoints(tmp_path, first, second):
    for conversation, ck in (first, second):
        _save(tmp_path, conversation, 30_000 if ck == FEW else 55_000, ck, ck_count=ck // CK)
    # The alias's own value is the last save's, whichever conversation that was.
    assert kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")["checkpoint_bytes"] == second[1]
    assert _ck(tmp_path, "c1") == FEW
    assert _ck(tmp_path, "c2") == MANY
    # The entry carries what was measured, for the conversation's next save.
    by_conversation = {e["conversation_id"]: e for e in kv.read_index(tmp_path)["entries"]}
    assert by_conversation["c1"]["checkpoint_bytes"] == FEW and by_conversation["c1"]["checkpoint_count"] == 4


def test_an_unseen_conversation_gets_the_median_over_the_aliass_samples(tmp_path):
    for conversation, n, tokens in (("c1", 4, 30_000), ("c2", 11, 55_000), ("c3", 7, 42_000)):
        _save(tmp_path, conversation, tokens, n * CK, ck_count=n)
    assert kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")["checkpoint_bytes"] == 7 * CK
    _save(tmp_path, "c4", 20_000, 2 * CK, ck_count=2)            # the alias's last value is now 2
    assert _ck(tmp_path, "c-new") == pytest.approx((4 * CK + 7 * CK) / 2)   # median of 4, 11, 7, 2


def test_an_entry_written_before_the_field_falls_back_to_the_median(tmp_path):
    _save(tmp_path, "c1", 30_000, 4 * CK, ck_count=4)
    _save(tmp_path, "c2", 55_000, 10 * CK, ck_count=10)
    index = kv.read_index(tmp_path)
    for e in index["entries"]:
        e.pop("checkpoint_bytes", None)
        e.pop("checkpoint_count", None)
    kv.write_index(tmp_path, index)
    assert _ck(tmp_path, "c1") == pytest.approx(7 * CK)


def test_an_index_with_no_samples_and_no_field_uses_the_aliass_value(tmp_path):
    kv.write_index(tmp_path, {"entries": [], "rates": {}, "sizes": {
        "qwen": {"floor_bytes": 0.3e9, "bytes_per_token": 20e3, "checkpoint_bytes": 5e8}}})
    assert _ck(tmp_path, "c1") == 5e8
    assert kv.alias_sizes(tmp_path, "qwen", "llamacpp_server", agent_id="a1", conversation_id="c1") == {
        "floor_bytes": 0.3e9, "bytes_per_token": 20e3, "checkpoint_bytes": 5e8}


def test_a_whole_file_fit_adds_no_checkpoints_however_the_conversation_was_saved(tmp_path):
    # The whole file's fit already counts whatever checkpoints its files held: adding any would count them twice.
    _save(tmp_path, "c1", 30_000, FEW, ck_count=4)
    _save(tmp_path, "c2", 55_000, None)
    assert "checkpoint_bytes" not in kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")
    assert _ck(tmp_path, "c1") is None


def test_another_agents_entry_of_the_same_conversation_id_is_not_this_conversation(tmp_path):
    _save(tmp_path, "c1", 30_000, MANY, ck_count=11)
    sizes = kv.alias_sizes(tmp_path, "qwen", "llamacpp_server", agent_id="a2", conversation_id="c1")
    assert sizes["checkpoint_bytes"] == MANY      # the median of the one sample, not the entry's own path
    _save(tmp_path, "c2", 55_000, FEW, ck_count=4)
    sizes = kv.alias_sizes(tmp_path, "qwen", "llamacpp_server", agent_id="a2", conversation_id="c1")
    assert sizes["checkpoint_bytes"] == pytest.approx((MANY + FEW) / 2)


@pytest.mark.asyncio
async def test_the_decision_line_estimates_with_the_conversations_own_checkpoints(tmp_path, caplog):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    _save(folder, "c1", 1_000, 2e8, ck_count=2, alias=ALIAS, identity=holder._kv_entry_identity())
    kv.write_index(folder, {**kv.read_index(folder), "rates": {}, "sizes": {
        ALIAS: {"floor_bytes": 1e6, "bytes_per_token": 1000.0, "checkpoint_bytes": 5e8, "checkpoint_count": 15}}})
    note_call()                                    # agent a1, conversation c1
    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await stop_by_hand(holder)

    line, = [r.getMessage() for r in caplog.records if "KV snapshot decision" in r.getMessage()]
    assert f"1500 tokens, estimate {int(1e6 + 1000 * 1500 + 2e8)} bytes" in line
