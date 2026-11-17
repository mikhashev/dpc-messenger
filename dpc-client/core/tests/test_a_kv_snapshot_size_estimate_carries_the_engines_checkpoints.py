"""A KV snapshot's size estimate carries the context checkpoints the engine wrote into the file.

A hybrid model's file is a floor, a part per token and `ctx_checkpoints` checkpoints of a fixed
size each. llama-server names their count and size in its own save line; the refit after a save
removes them from the file before it fits the part per token, and the alias keeps the last measured
checkpoint bytes, which every estimate adds. No engine line (Strata names no checkpoint bytes, a log
that did not reach the tail), or an index written before the field: exactly the fit of before.
Live case of 2026-11-17 00:19: 67,797 tokens, 4,041,495,004 bytes, 15 checkpoints = 2244.686 MiB.
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
LIVE_BYTES = 4_041_495_004
LIVE_TOKENS = 67_797
LIVE_CK_MIB = "2244.686"
LIVE_CK_BYTES = round(float(LIVE_CK_MIB) * MIB)
LLAMA_FLOOR = kv.SEED_SIZES["llamacpp_server"]["floor_bytes"]
LLAMA_LINE = ("189.58.475.647 I slot save_slot_ch: id  0 | task -1 | appended {n} context checkpoint(s) "
              "({mib} MiB) to 'D:\\kv\\qwen\\{name}'")
STRATA_LINE = ("strata serve: session saved 82754 tokens, 1 of 6 checkpoints, 1499976756 bytes to "
               "D:\\kv\\flash\\{name} in 14580.0 ms (state 60.1 ms)")


def _entry(folder, *, nbytes=LIVE_BYTES, n_saved=LIVE_TOKENS, save_ms=33_156, engine="llamacpp_server",
           alias="qwen", conversation="c1"):
    _, final = kv.new_snapshot_names(folder)
    (folder / final).write_bytes(b"x" * 8)
    record = {"agent_id": "a1", "conversation_id": conversation, "caller_kind": "agent",
              "prefix_sha256": "ab", "prefix_messages": 2}
    return kv.new_entry(file=final, alias=alias, engine=engine, identity={"engine_version": "1"},
                        record=record, n_saved=n_saved, nbytes=nbytes, save_ms=save_ms, now=time.time())


def _fit(held):
    """A held fit without its size samples, which every save now records."""
    return {k: v for k, v in held.items() if k != "size_samples"}


def _estimate(sizes, tokens):
    return kv.estimate_bytes(tokens, sizes["floor_bytes"], sizes["bytes_per_token"],
                             sizes.get("checkpoint_bytes", 0.0))


# ---- the engine's save line as numbers

def test_the_engines_save_line_is_read_as_a_count_and_bytes():
    name = "dpc-kv-" + "1" * 32 + ".staging"
    line = LLAMA_LINE.format(n=15, mib=LIVE_CK_MIB, name=name)
    assert kv.engine_checkpoints_of(line, name) == (15, LIVE_CK_BYTES)
    # The words of the at-save line are what they were.
    assert kv.engine_checkpoints_words(line, name) == f"engine_checkpoints 15 ({LIVE_CK_MIB} MiB)"
    # Strata names the count, not the checkpoints' bytes.
    assert kv.engine_checkpoints_of(STRATA_LINE.format(name=name), name) == (1, None)
    assert kv.engine_checkpoints_of(line, "dpc-kv-" + "2" * 32 + ".staging") is None
    assert kv.engine_checkpoints_of("", name) is None and kv.engine_checkpoints_of(line, "") is None


# ---- the refit

def test_the_live_save_fits_the_part_per_token_without_the_checkpoints(tmp_path):
    assert kv.add_entry(tmp_path, _entry(tmp_path), cap_bytes=10 ** 12,
                        checkpoint_count=15, checkpoint_bytes=LIVE_CK_BYTES)
    held = kv.read_index(tmp_path)["sizes"]["qwen"]
    assert held["checkpoint_bytes"] == LIVE_CK_BYTES and held["checkpoint_count"] == 15
    sizes = kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")
    assert sizes["floor_bytes"] == LLAMA_FLOOR
    assert sizes["bytes_per_token"] == pytest.approx((LIVE_BYTES - LIVE_CK_BYTES - LLAMA_FLOOR) / LIVE_TOKENS)
    # Not the ~52.7 KB/token the whole file gives: the seed floor already holds two checkpoints, so it is lower.
    assert sizes["bytes_per_token"] < kv.SEED_SIZES["llamacpp_server"]["bytes_per_token"]
    assert _estimate(sizes, LIVE_TOKENS) == pytest.approx(LIVE_BYTES, rel=0.01)
    # A short conversation still carries the checkpoints, which do not shrink with the length.
    assert _estimate(sizes, 5_000) > LIVE_CK_BYTES + LLAMA_FLOOR


def test_a_save_without_an_engine_line_fits_exactly_as_before(tmp_path):
    assert kv.add_entry(tmp_path, _entry(tmp_path), cap_bytes=10 ** 12)
    assert _fit(kv.read_index(tmp_path)["sizes"]["qwen"]) == {
        "floor_bytes": LLAMA_FLOOR, "bytes_per_token": (LIVE_BYTES - LLAMA_FLOOR) / LIVE_TOKENS}
    assert "checkpoint_bytes" not in kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")


def test_a_later_save_without_the_line_drops_the_held_checkpoints(tmp_path):
    kv.add_entry(tmp_path, _entry(tmp_path), cap_bytes=10 ** 12, checkpoint_count=15, checkpoint_bytes=LIVE_CK_BYTES)
    kv.add_entry(tmp_path, _entry(tmp_path, conversation="c2", nbytes=3_000_000_000, n_saved=50_000),
                 cap_bytes=10 ** 12)
    sizes = kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")
    assert "checkpoint_bytes" not in sizes
    assert _estimate(sizes, 50_000) == 3_000_000_000


def test_strata_counts_without_bytes_fit_as_before(tmp_path):
    floor = kv.SEED_SIZES["strata"]["floor_bytes"]
    kv.add_entry(tmp_path, _entry(tmp_path, engine="strata", nbytes=1_499_976_756, n_saved=82_754),
                 cap_bytes=10 ** 12, checkpoint_count=1, checkpoint_bytes=None)
    assert _fit(kv.read_index(tmp_path)["sizes"]["qwen"]) == {
        "floor_bytes": floor, "bytes_per_token": (1_499_976_756 - floor) / 82_754}


@pytest.mark.parametrize("bad", [LIVE_BYTES, LIVE_BYTES + 1, -1, float("nan"), True, "2244"])
def test_checkpoint_bytes_that_cannot_be_part_of_the_file_are_ignored(tmp_path, bad):
    kv.add_entry(tmp_path, _entry(tmp_path), cap_bytes=10 ** 12, checkpoint_count=15, checkpoint_bytes=bad)
    assert _fit(kv.read_index(tmp_path)["sizes"]["qwen"]) == {
        "floor_bytes": LLAMA_FLOOR, "bytes_per_token": (LIVE_BYTES - LLAMA_FLOOR) / LIVE_TOKENS}


def test_checkpoints_that_leave_the_rest_below_the_floor_fall_back_to_the_whole_file(tmp_path):
    # 600 MB at 5K tokens with 2 checkpoints of 150 MiB: the rest is under the 0.47 GB seed floor.
    kv.add_entry(tmp_path, _entry(tmp_path, nbytes=600_000_000, n_saved=5_000), cap_bytes=10 ** 12,
                 checkpoint_count=2, checkpoint_bytes=round(299.291 * MIB))
    assert _fit(kv.read_index(tmp_path)["sizes"]["qwen"]) == {
        "floor_bytes": LLAMA_FLOOR, "bytes_per_token": (600_000_000 - LLAMA_FLOOR) / 5_000}


def test_an_index_written_before_the_field_loads_and_estimates_as_before(tmp_path):
    kv.write_index(tmp_path, {"entries": [], "rates": {}, "sizes": {
        "qwen": {"floor_bytes": 0.3e9, "bytes_per_token": 20e3}}})
    sizes = kv.alias_sizes(tmp_path, "qwen", "llamacpp_server")
    assert sizes == {"floor_bytes": 0.3e9, "bytes_per_token": 20e3}
    assert kv.read_index(tmp_path)["version"] == kv.INDEX_VERSION == 1


# ---- the estimate

def test_the_estimate_adds_the_held_checkpoint_bytes():
    assert kv.estimate_bytes(1_000, 1e6, 10.0) == 1_010_000
    assert kv.estimate_bytes(1_000, 1e6, 10.0, 5e8) == 501_010_000


def test_worth_saving_counts_the_checkpoints_in_the_restore():
    rates = {"write_bytes_per_s": 50e6, "read_bytes_per_s": 50e6}    # an HDD
    sizes = {"floor_bytes": 0.47e9, "bytes_per_token": 18e3}
    tokens, cold = 67_797, 1_332.0                    # cold read ~50.9 s
    plain = (0.47e9 + 18e3 * tokens) / 50e6
    assert kv.worth_saving(tokens, rates, sizes, cold) == (
        True, f"restore ~{plain:.1f} s against a cold read ~{tokens / cold:.1f} s")
    # Slower than the cold read with the checkpoints, and saved all the same since 2026-11-17.
    ok, words = kv.worth_saving(tokens, rates, {**sizes, "checkpoint_bytes": float(LIVE_CK_BYTES)}, cold)
    assert ok and f"restore ~{plain + LIVE_CK_BYTES / 50e6:.1f} s" in words


# ---- through the supervisor's save

def _seed(folder, sizes, rates=None):
    folder.mkdir(parents=True, exist_ok=True)
    kv.write_index(folder, {**kv.read_index(folder), "rates": rates or {}, "sizes": {ALIAS: sizes}})


@pytest.mark.asyncio
async def test_a_save_refits_on_the_file_without_the_checkpoints_the_engine_named(tmp_path):
    size = 32 * MIB                                   # big and slow enough to be a rate sample
    holder, _new, fake, folder = await saving_pair(tmp_path, size=size, delay=0.06)
    _seed(folder, {"floor_bytes": 1e6, "bytes_per_token": 1.0})
    real = fake.action

    async def save_and_log(slot, action, filename, timeout):
        result = await real(slot, action, filename, timeout)
        holder._log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(holder._log_path, "a", encoding="utf-8") as f:
            f.write(LLAMA_LINE.format(n=4, mib="16.000", name=filename) + "\n")
        return result

    holder._slot_action = save_and_log
    note_call()
    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == kv.SAVED
    held = kv.read_index(folder)["sizes"][ALIAS]
    assert held["bytes_per_token"] == pytest.approx((size - 16 * MIB - 1e6) / 1500), held
    assert held["checkpoint_bytes"] == 16 * MIB and held["checkpoint_count"] == 4


@pytest.mark.asyncio
async def test_a_save_with_no_engine_line_refits_as_before(tmp_path):
    size = 32 * MIB
    holder, _new, fake, folder = await saving_pair(tmp_path, size=size, delay=0.06)
    _seed(folder, {"floor_bytes": 1e6, "bytes_per_token": 1.0, "checkpoint_bytes": 5e6, "checkpoint_count": 2})
    note_call()
    outcome = await stop_by_hand(holder)

    assert outcome["outcome"] == kv.SAVED
    assert _fit(kv.read_index(folder)["sizes"][ALIAS]) == {"floor_bytes": 1e6, "bytes_per_token": (size - 1e6) / 1500}


@pytest.mark.asyncio
async def test_the_decision_line_estimate_carries_the_held_checkpoint_bytes(tmp_path, caplog):
    holder, _new, fake, folder = await saving_pair(tmp_path)
    _seed(folder, {"floor_bytes": 1e6, "bytes_per_token": 1000.0, "checkpoint_bytes": 5e8, "checkpoint_count": 15})
    note_call()
    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await stop_by_hand(holder)

    line, = [r.getMessage() for r in caplog.records if "KV snapshot decision" in r.getMessage()]
    assert f"1500 tokens, estimate {int(1e6 + 1000 * 1500 + 5e8)} bytes" in line
