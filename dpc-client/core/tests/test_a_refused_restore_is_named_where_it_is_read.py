"""A refused host-cache restore is said where somebody reads it.

Nothing in this package asks the engine to restore a parked prefix: the
enumeration of every endpoint we touch is /health, /props, /slots (read) and
the chat path. The engine tries the restore on its own prompt-cache lookup and
logs only the failure, into a per-alias file of ~100K lines. So the thing that
was missing on this path was never a refusal — it was a reading.

The log text below is copied from this machine's own child logs, not written
to fit the parser: the 126546/2491921556 block is from
`llama-server-qwen3.8 27b Mythos.log`, the 150145/2927370304 one from the
pre-rename `llama-server-llama.cpp.log`.
"""

import logging

import pytest

from dpc_client_core.managers.llama_server_supervisor import (
    LlamaServerSupervisor,
    format_restore_refusal,
)

GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"

REFUSAL = (
    "13.52.863.004 E state_read_meta: failed to find 126546 available cells in kv cache\n"
    "13.52.864.488 E state_seq_set_data: error loading state: failed to restore kv cache\n"
    "13.52.864.495 E srv          load: failed to restore state with size 2491921556\n"
    "13.52.864.496 W slot  prompt_load: id  1 | task -1 | failed to load prompt from cache\n"
)

OLDER_REFUSAL = (
    "19.21.324.500 E state_read_meta: failed to find 150145 available cells in kv cache\n"
    "19.21.327.488 E state_seq_set_data: error loading state: failed to restore kv cache\n"
    "19.21.327.495 E srv          load: failed to restore state with size 2927370304\n"
    "19.21.327.496 W slot  prompt_load: id  3 | task -1 | failed to load prompt from cache\n"
)

ORDINARY = (
    "13.52.100.000 I srv    load_model: initializing, n_slots = 4, n_ctx_slot = 262144, kv_unified = 'true'\n"
    "13.52.200.000 I slot   operator(): id  2 | task 7 | new prompt, n_ctx_slot = 262144\n"
)


def _sup(tmp_path, contents="", **overrides):
    config = {"gguf_path": GGUF}
    config.update(overrides)
    sup = LlamaServerSupervisor("local_qwen38", config)
    log = tmp_path / "llama-server-local_qwen38.log"
    log.write_text(contents, encoding="utf-8")
    sup._log_path = log
    return sup, log


def _append(log, text):
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


class TestTheScanReadsWhatTheEngineWrote:
    def test_a_production_block_is_parsed_whole(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        _append(log, REFUSAL)

        found = sup.new_restore_refusals()

        assert len(found) == 1
        assert found[0]["cells"] == 126546
        assert found[0]["bytes"] == 2491921556
        assert found[0]["slot"] == 1

    def test_two_refusals_arrive_oldest_first(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        _append(log, REFUSAL + ORDINARY + OLDER_REFUSAL)

        found = sup.new_restore_refusals()

        assert [f["cells"] for f in found] == [126546, 150145]
        assert [f["slot"] for f in found] == [1, 3]

    def test_a_quiet_log_reports_nothing(self, tmp_path):
        sup, _ = _sup(tmp_path, ORDINARY * 20)
        sup.prime_restore_scan()

        assert sup.new_restore_refusals() == []

    def test_a_missing_log_is_not_an_error(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        log.unlink()

        assert sup.new_restore_refusals() == []


class TestItIsSaidOncePerEvent:
    """The tail is re-read after every call, so the same four lines are in
    view for the rest of the child's life."""

    def test_the_same_refusal_is_reported_once(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        _append(log, REFUSAL)

        assert len(sup.new_restore_refusals()) == 1
        assert sup.new_restore_refusals() == []
        assert sup.new_restore_refusals() == []

    def test_a_later_refusal_is_still_reported(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        _append(log, REFUSAL)
        sup.new_restore_refusals()

        _append(log, ORDINARY + OLDER_REFUSAL)
        found = sup.new_restore_refusals()

        assert [f["cells"] for f in found] == [150145]


class TestAPreviousChildsRefusalIsNotThisOnes:
    """The log is opened `ab` and outlives restarts. Reporting what the last
    child suffered as this one's is the stale-evidence class this project
    keeps paying for."""

    def test_priming_at_launch_excludes_what_was_already_there(self, tmp_path):
        sup, _ = _sup(tmp_path, ORDINARY + REFUSAL + ORDINARY)
        sup.prime_restore_scan()

        assert sup.new_restore_refusals() == []

    def test_an_unprimed_scan_watches_from_now_rather_than_from_the_past(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY + REFUSAL)

        assert sup.new_restore_refusals() == []

        _append(log, OLDER_REFUSAL)
        assert [f["cells"] for f in sup.new_restore_refusals()] == [150145]


class TestATruncatedLogDoesNotSilenceTheScan:
    def test_a_shrunken_file_is_watched_from_its_first_byte(self, tmp_path):
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        _append(log, ORDINARY * 40 + REFUSAL)
        assert len(sup.new_restore_refusals()) == 1

        log.write_text(OLDER_REFUSAL, encoding="utf-8")
        found = sup.new_restore_refusals()

        assert [f["cells"] for f in found] == [150145]


class TestTheSentenceCarriesTheArithmetic:
    """What the engine does not say: a restore that wanted W cells out of a
    P-cell pool failed because fewer than W were free."""

    def test_it_states_what_was_held_elsewhere(self):
        sentence = format_restore_refusal(
            {"cells": 126546, "bytes": 2491921556, "slot": 1}, 262144
        )

        assert "126546 cells wanted against a 262144-cell pool" in sentence
        assert "at least 135598" in sentence
        assert "slot 1" in sentence
        assert "2376 MiB" in sentence

    def test_a_state_larger_than_the_pool_says_that_instead(self):
        sentence = format_restore_refusal({"cells": 300000, "bytes": None, "slot": None}, 262144)

        assert "cannot fit whatever else is running" in sentence
        assert "at least" not in sentence
        assert "unknown size" in sentence


class TestTheWarningReachesTheLogPeopleRead:
    def test_it_names_the_alias_and_the_numbers(self, tmp_path, caplog):
        sup, log = _sup(tmp_path, ORDINARY, n_ctx=262144)
        sup.prime_restore_scan()
        _append(log, REFUSAL)

        with caplog.at_level(
            logging.WARNING, logger="dpc_client_core.managers.llama_server_supervisor"
        ):
            said = sup.log_restore_refusals()

        assert said == 1
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "local_qwen38" in line
        assert "host-cache restore refused" in line
        assert "126546" in line

    def test_a_quiet_child_says_nothing_at_all(self, tmp_path, caplog):
        sup, _ = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()

        with caplog.at_level(
            logging.WARNING, logger="dpc_client_core.managers.llama_server_supervisor"
        ):
            assert sup.log_restore_refusals() == 0
        assert caplog.records == []


class TestTheProviderActuallyAsks:
    """A reading nobody calls is not a reading. Both sides here are the real
    objects: a fake supervisor shaped like this call site would agree with the
    call site even when the call site is missing."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

        _ACTIVE_SUPERVISORS.clear()
        yield
        _ACTIVE_SUPERVISORS.clear()

    def test_recording_a_call_says_what_the_child_refused(self, tmp_path, caplog):
        from types import SimpleNamespace

        from dpc_client_core.providers import LlamaServerProvider

        provider = LlamaServerProvider(
            "local_qwen38", {"type": "llamacpp_server", "gguf_path": GGUF}
        )
        sup, log = _sup(tmp_path, ORDINARY)
        sup.prime_restore_scan()
        provider.supervisor = sup
        _append(log, REFUSAL)

        with caplog.at_level(
            logging.WARNING, logger="dpc_client_core.managers.llama_server_supervisor"
        ):
            provider._record_usage(
                SimpleNamespace(prompt_tokens=1000, completion_tokens=10, total_tokens=1010),
                path="tools",
            )

        assert any("host-cache restore refused" in r.getMessage() for r in caplog.records)


class TestTheSharedTailDidNotBreakTheTimings:
    """`last_task_timings` moved onto the same reader; it used to own its
    file handling. Its own suite covers the parse — this covers the seam."""

    def test_the_timings_still_come_out_of_the_shared_reader(self, tmp_path):
        timings_text = (
            "13.55.1 I slot print_timing: id 3 | task 2717 | prompt eval time =    3693.67 ms /  1621 tokens (    2.28 ms per token,   438.86 tokens per second)\n"
            "13.55.1 I slot print_timing: id 3 | task 2717 |        eval time =   23196.43 ms /   757 tokens (   30.68 ms per token,    32.59 tokens per second)\n"
        )
        sup, _ = _sup(tmp_path, ORDINARY + timings_text + REFUSAL)

        timings = sup.last_task_timings()

        assert timings["prefill_tok_s"] == 438
        assert timings["engine_prompt_tokens"] == 1621
