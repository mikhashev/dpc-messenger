"""A KV snapshot logs a fingerprint of the messages it saved and of the request that tries to
restore it, so a "prefix moved" can be traced to the message indices that differ.

Measurement only: `kv_snapshots.message_fingerprints` (short sha256 per message, the index just
after the last "user" message, message 0's canonical length), kept in the in-memory call record,
logged beside a successful save and at every restore attempt that found an index entry. The
matching, the deletion and the outcomes are the ones the other KV snapshot tests pin; the
fingerprint is never written to the index.

The save line also says how many engine calls of the conversation shared the turn base
before the save (`calls_in_turn`) and how many context checkpoints the engine wrote into the
file (`engine_checkpoints`, from the engine's own log line naming the staging file): the
engine keeps only `ctx_checkpoints` of them, so a turn of more calls loses its base.
"""

import hashlib
import json
import logging
import time

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers import llama_server_supervisor as base_mod

from tests.test_a_kv_snapshot_is_restored_only_into_an_empty_engine_for_its_own_conversation import (  # noqa: F401
    ALIAS as RESTORE_ALIAS, TURN_2, RestoringSupervisor, _files, _saved, _turn, clean,
)
from tests.test_a_swap_saves_the_owners_live_conversation_before_the_stop import (  # noqa: F401
    ALIAS, card, index_entries, kv_state, saving_pair, settings, swap_on,
)
from tests.test_one_local_engine_holds_the_card import _strata

PROVIDER_LOGGER = "dpc_client_core.providers.llamacpp_server_provider"

SAMPLE = [
    {"role": "system", "content": "you are a helper"},
    {"role": "user", "content": "list the files"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "ls"}}]},
    {"role": "tool", "tool_call_id": "1", "content": "a.txt"},
    {"role": "user", "content": "open a.txt"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "function": {"name": "cat"}}]},
    {"role": "tool", "tool_call_id": "2", "content": "hello"},
]


def _short(message):
    text = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def test_the_fingerprint_has_a_short_hash_per_message_and_a_stable_value():
    fp = kv.message_fingerprints(SAMPLE)
    assert fp["hashes"] == [_short(m) for m in SAMPLE]
    assert all(len(h) == 8 for h in fp["hashes"])
    assert kv.message_fingerprints(list(SAMPLE)) == fp
    # Key order inside a message does not change its hash (canonical JSON).
    shuffled = [dict(reversed(list(SAMPLE[1].items())))] + SAMPLE[2:]
    assert kv.message_fingerprints(shuffled)["hashes"][0] == fp["hashes"][1]


def test_the_base_ends_just_after_the_last_user_message_even_with_tool_messages_after_it():
    assert kv.message_fingerprints(SAMPLE)["base_end"] == 5
    assert kv.message_fingerprints(SAMPLE[:4])["base_end"] == 2
    assert kv.message_fingerprints(SAMPLE[:1])["base_end"] == 0
    assert kv.message_fingerprints([])["base_end"] == 0


def test_message_zero_length_is_its_canonical_json_and_an_empty_list_has_none():
    fp = kv.message_fingerprints(SAMPLE)
    assert fp["msg0_chars"] == len(json.dumps(SAMPLE[0], sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False))
    assert kv.message_fingerprints([])["msg0_chars"] == 0 and kv.message_fingerprints(None)["hashes"] == []


def test_the_fingerprint_words_name_the_count_the_base_and_the_hashes():
    words = kv.fingerprint_words(kv.message_fingerprints(SAMPLE))
    assert words.startswith("messages=7 base_end=5 msg0_chars=")
    assert words.endswith("hashes=" + ",".join(_short(m) for m in SAMPLE))


def test_the_fingerprint_stays_in_memory_and_never_reaches_the_index_entry():
    fp = kv.message_fingerprints(SAMPLE)
    seq = kv.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                              prefix=kv.prefix_digest(SAMPLE), fingerprint=fp, at=time.time())
    assert kv.mark_sent(ALIAS, seq)
    record = kv.engine_call_of(ALIAS)
    assert record["fingerprint"] == fp
    entry = kv.new_entry(file="dpc-kv-ab.bin", alias=ALIAS, engine="llamacpp_server", identity={},
                         record=record, n_saved=1, nbytes=1, save_ms=1, now=time.time())
    assert "fingerprint" not in entry and "hashes" not in json.dumps(entry)
    kv.reset_state()


@pytest.mark.asyncio
async def test_a_successful_save_logs_the_prefix_of_the_call_it_saved(tmp_path, caplog):
    holder, new, fake, folder = await saving_pair(tmp_path)
    fp = kv.message_fingerprints(SAMPLE)
    kv.note_clearable("c1")
    seq = kv.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                              prefix=kv.prefix_digest(SAMPLE), fingerprint=fp, at=time.time())
    assert kv.mark_sent(ALIAS, seq)

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    lines = [r.getMessage() for r in caplog.records if "KV snapshot prefix at save:" in r.getMessage()]
    assert lines == [f"llama-server[{ALIAS}]: KV snapshot prefix at save: agent a1 conversation c1 "
                     + kv.fingerprint_words(fp) + " calls_in_turn 1 engine_checkpoints unknown"]
    entry, = index_entries(folder)
    assert "fingerprint" not in entry and "calls_in_turn" not in entry


# ---- calls_in_turn and engine_checkpoints: the checkpoint-count probe, measurement only

LLAMA_LINE = ("189.58.475.647 I slot save_slot_ch: id  0 | task -1 | appended 2 context checkpoint(s) "
              "(299.291 MiB) to 'D:\\kv\\qwen\\{name}'")
LLAMA_DRAFT = ("190.02.198.763 I slot save_slot_dr: id  0 | task -1 | appended the draft state (368.683 MiB) "
               "to 'D:\\kv\\qwen\\{name}'")
STRATA_LINE = ("strata serve: session saved 82754 tokens, 1 of 6 checkpoints, 1499976756 bytes to "
               "D:\\kv\\flash\\{name} in 14580.0 ms (state 60.1 ms)")


def _send(conversation="c1", prefix=("ab" * 32, 3), alias=ALIAS):
    seq = kv.note_engine_call(alias, caller_kind="agent", agent_id="a1", conversation_id=conversation,
                              prefix=prefix, at=time.time())
    assert kv.mark_sent(alias, seq)
    return kv.engine_call_of(alias)["calls_in_turn"]


def test_calls_of_one_turn_are_counted_and_the_count_restarts_when_the_base_or_conversation_changes():
    assert [_send(), _send(), _send()] == [1, 2, 3]
    assert _send(prefix=("cd" * 32, 5)) == 1
    assert _send(prefix=("cd" * 32, 5)) == 2
    assert _send(conversation="c2", prefix=("cd" * 32, 5)) == 1
    assert _send(conversation="c1", prefix=("cd" * 32, 5)) == 3   # c2's call between did not break c1's chain
    # Another alias keeps its own count; a call with no conversation or no prefix is always 1.
    assert _send(alias="other") == 1
    assert _send(conversation=None) == 1 and _send(conversation=None) == 1
    assert _send(prefix=None) == 1 and _send(prefix=None) == 1


def test_another_agents_call_on_the_alias_does_not_restart_a_conversations_count():
    warren = dict(conversation="warren-c")
    other = dict(conversation="other-c", prefix=("cd" * 32, 5))
    assert _send(**warren) == 1
    assert _send(**other) == 1          # the alias's last call is now the other agent's
    assert _send(**warren) == 2
    assert _send(**other) == 2          # its own chain, independent of Warren's
    assert _send(**warren) == 3


def test_a_pending_call_is_not_counted_until_it_is_sent():
    assert _send() == 1
    kv.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                        prefix=("ab" * 32, 3), at=time.time())   # noted, never sent
    assert _send() == 2


def test_the_engines_save_line_is_read_for_its_own_staging_file_only():
    name, other = "dpc-kv-" + "1" * 32 + ".staging", "dpc-kv-" + "2" * 32 + ".staging"
    llama = "\n".join([LLAMA_LINE.format(name=other), LLAMA_LINE.format(name=name).replace("2 context", "3 context"),
                       LLAMA_DRAFT.format(name=name)])
    assert kv.engine_checkpoints_words(llama, name) == "engine_checkpoints 3 (299.291 MiB)"
    assert kv.engine_checkpoints_words(llama, other) == "engine_checkpoints 2 (299.291 MiB)"
    assert kv.engine_checkpoints_words(llama, "dpc-kv-" + "3" * 32 + ".staging") is None
    assert kv.engine_checkpoints_words(STRATA_LINE.format(name=name), name) == "engine_checkpoints 1 of 6"
    assert kv.engine_checkpoints_words(LLAMA_DRAFT.format(name=name), name) is None
    assert kv.engine_checkpoints_words("", name) is None and kv.engine_checkpoints_words(llama, "") is None


@pytest.mark.asyncio
async def test_the_save_line_counts_the_turns_calls_and_the_checkpoints_the_engine_wrote(tmp_path, caplog):
    holder, new, fake, folder = await saving_pair(tmp_path)
    real = fake.action

    async def save_and_log(slot, action, filename, timeout):
        result = await real(slot, action, filename, timeout)
        holder._log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(holder._log_path, "a", encoding="utf-8") as f:
            f.write(LLAMA_LINE.format(name=filename) + "\n" + LLAMA_DRAFT.format(name=filename) + "\n")
        return result

    holder._slot_action = save_and_log
    fp = kv.message_fingerprints(SAMPLE)
    kv.note_clearable("c1")
    for _ in range(3):
        seq = kv.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                                  prefix=kv.prefix_digest(SAMPLE, 5), fingerprint=fp, at=time.time())
        assert kv.mark_sent(ALIAS, seq)

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    line, = [r.getMessage() for r in caplog.records if "KV snapshot prefix at save:" in r.getMessage()]
    assert line.endswith(kv.fingerprint_words(fp) + " calls_in_turn 3 engine_checkpoints 2 (299.291 MiB)")


@pytest.mark.asyncio
async def test_strata_reads_the_checkpoints_from_its_engine_log(tmp_path):
    sup = _strata(tmp_path, "flash")
    name = "dpc-kv-" + "4" * 32 + ".staging"
    assert sup._kv_engine_checkpoints_words(name) == "engine_checkpoints unknown"
    sup._engine_log_path.parent.mkdir(parents=True, exist_ok=True)
    sup._engine_log_path.write_text("noise\n" + STRATA_LINE.format(name=name) + "\nmore noise\n", encoding="utf-8")
    assert sup._kv_engine_checkpoints_words(name) == "engine_checkpoints 1 of 6"


@pytest.mark.asyncio
async def test_a_save_of_a_record_without_a_fingerprint_logs_no_prefix_line(tmp_path, caplog):
    holder, new, fake, folder = await saving_pair(tmp_path)
    kv.note_clearable("c1")
    seq = kv.note_engine_call(ALIAS, caller_kind="agent", agent_id="a1", conversation_id="c1",
                              prefix=("ab" * 32, 3), at=time.time())
    assert kv.mark_sent(ALIAS, seq)

    with caplog.at_level(logging.INFO, logger=base_mod.logger.name):
        await new.ensure_running()

    assert any("saved 4096 bytes in" in r.getMessage() for r in caplog.records)
    assert not any("KV snapshot prefix at save" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_restore_attempt_logs_the_new_requests_prefix_with_the_file_and_the_saved_k(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    saved_k = kv.read_index(folder)["entries"][0]["prefix_messages"]
    with caplog.at_level("INFO", logger=PROVIDER_LOGGER):
        await _turn(p, TURN_2)

    lines = [r.getMessage() for r in caplog.records if "KV snapshot prefix at restore:" in r.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith(f"{RESTORE_ALIAS}: KV snapshot prefix at restore: agent agent_7 conversation conv-a "
                           f"file {final} saved_k {saved_k} messages=")
    assert "base_end=" in line and "msg0_chars=" in line and "hashes=" in line
    # The outcome is the one it always was: the prefix held, the file restored and kept.
    assert [r["file"] for r in sup.restores] == [final]
    assert _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_moved_prefix_is_still_deleted_and_the_restore_line_comes_before_the_verdict(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    moved = [{"role": "user", "content": "a different first message"}] + TURN_2[1:]
    with caplog.at_level("INFO", logger=PROVIDER_LOGGER):
        await _turn(p, moved)

    texts = [r.getMessage() for r in caplog.records]
    at = next(i for i, t in enumerate(texts) if "KV snapshot prefix at restore:" in t)
    verdict = next(i for i, t in enumerate(texts) if "not restored: the prefix moved" in t)
    assert at < verdict
    assert sup.restores == [] and _files(folder) == []


@pytest.mark.asyncio
async def test_no_index_entry_for_the_caller_keeps_the_restore_path_silent(tmp_path, caplog):
    p, sup, _, folder, final = await _saved(tmp_path)
    with caplog.at_level("INFO", logger=PROVIDER_LOGGER):
        await _turn(p, TURN_2, conversation_id="conv-b")

    assert not any("KV snapshot prefix at restore" in r.getMessage() for r in caplog.records)
    assert sup.restores == [] and _files(folder) == [final]


@pytest.mark.asyncio
async def test_a_fingerprint_that_cannot_be_made_only_skips_its_line(tmp_path, caplog, monkeypatch):
    p, sup, _, folder, final = await _saved(tmp_path)

    def broken(messages):
        raise RuntimeError("no fingerprint today")

    monkeypatch.setattr(kv, "message_fingerprints", broken)
    with caplog.at_level("INFO", logger=PROVIDER_LOGGER):
        await _turn(p, TURN_2)

    assert not any("KV snapshot prefix at restore" in r.getMessage() for r in caplog.records)
    assert [r["file"] for r in sup.restores] == [final]
    assert kv.engine_call_of(RESTORE_ALIAS)["fingerprint"] is None
