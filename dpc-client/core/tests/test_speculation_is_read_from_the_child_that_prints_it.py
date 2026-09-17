"""What MTP buys is printed per task and has never been read.

The one measurement behind our draft depth was a single synthetic prompt on
2026-08-19 (acceptance 0.686 at n=3 against 0.578 at n=4), and it set the
default in `llama_server_supervisor.py`. Meanwhile the child prints its own
counters after every task into a log nobody opens.

Every line below is copied verbatim from this machine's own
`llama-server-qwen3.8 27b Mythos.log`, not written to fit the parser.
"""

import logging

import pytest

from dpc_client_core.managers.llama_server_supervisor import (
    LlamaServerSupervisor,
    _parse_draft,
)

GGUF = "D:/models/qwen3.8-27b-Q4_K_M.gguf"

DRAFT_N3 = (
    "19.32.297.174 I slot print_timing: id  1 | task 6453 | draft acceptance = "
    "0.58174 ( 5206 accepted /  8949 generated), mean len =  2.74\n"
)
DRAFT_N4 = (
    "1.07.530.654 I slot print_timing: id  3 | task 328 | draft acceptance = "
    "0.92647 (   63 accepted /    68 generated), mean len =  4.71\n"
)
DRAFT_N7 = (
    "3.15.127.278 I slot print_timing: id  2 | task 109 | draft acceptance = "
    "0.53297 (   97 accepted /   182 generated), mean len =  4.73\n"
)

TIMINGS = (
    "13.55.1 I slot print_timing: id 3 | task 2717 | prompt eval time =    3693.67 ms /  1621 tokens (    2.28 ms per token,   438.86 tokens per second)\n"
    "13.55.1 I slot print_timing: id 3 | task 2717 |        eval time =   23196.43 ms /   757 tokens (   30.68 ms per token,    32.59 tokens per second)\n"
)

ORDINARY = (
    "13.52.100.000 I srv    load_model: initializing, n_slots = 4, n_ctx_slot = 262144\n"
)


def _sup(tmp_path, contents=""):
    sup = LlamaServerSupervisor("local_qwen38", {"gguf_path": GGUF})
    log = tmp_path / "llama-server-local_qwen38.log"
    log.write_text(contents, encoding="utf-8")
    sup._log_path = log
    return sup, log


class TestTheCountersAreReadAsWritten:
    def test_a_production_line_is_parsed_whole(self):
        d = _parse_draft(DRAFT_N3)

        assert d["draft_acceptance"] == pytest.approx(0.58174)
        assert d["draft_accepted"] == 5206
        assert d["draft_generated"] == 8949
        assert d["draft_tokens_per_pass"] == pytest.approx(2.74)

    def test_the_last_task_is_the_one_reported(self):
        d = _parse_draft(DRAFT_N3 + ORDINARY + DRAFT_N4)

        assert d["draft_accepted"] == 63


class TestTheDepthIsRecoveredBecauseNothingElseRecordsIt:
    """`mean len` = acceptance x n_max + 1, so a finished child can be asked
    which depth it ran with — the command line is gone by then and neither log
    records the value."""

    @pytest.mark.parametrize("line,expected", [(DRAFT_N3, 3), (DRAFT_N4, 4), (DRAFT_N7, 7)])
    def test_the_depth_comes_out_of_the_identity(self, line, expected):
        assert _parse_draft(line)["draft_n_max"] == expected

    def test_a_line_that_does_not_resolve_reports_no_depth(self):
        odd = (
            "1.00.000.000 I slot print_timing: id  0 | task 1 | draft acceptance = "
            "0.50000 (   10 accepted /    20 generated), mean len =  2.90\n"
        )

        d = _parse_draft(odd)

        assert "draft_n_max" not in d
        assert d["draft_acceptance"] == pytest.approx(0.5)


class TestNotDraftingIsNotDraftingNothing:
    def test_a_child_without_speculation_reports_no_keys_at_all(self):
        assert _parse_draft(ORDINARY * 10) == {}

    def test_a_zero_acceptance_still_reports_the_counters(self):
        zero = (
            "1.00.000.000 I slot print_timing: id  0 | task 1 | draft acceptance = "
            "0.00000 (    0 accepted /   120 generated), mean len =  1.00\n"
        )

        d = _parse_draft(zero)

        assert d["draft_generated"] == 120
        assert d["draft_acceptance"] == 0.0
        assert "draft_n_max" not in d


class TestOneTailReadCarriesBoth:
    def test_the_timings_reader_returns_the_speculation_too(self, tmp_path):
        sup, _ = _sup(tmp_path, ORDINARY + TIMINGS + DRAFT_N4)

        t = sup.last_task_timings()

        assert t["prefill_tok_s"] == 438
        assert t["draft_n_max"] == 4
        assert t["draft_tokens_per_pass"] == pytest.approx(4.71)

    def test_timings_without_speculation_are_still_timings(self, tmp_path):
        sup, _ = _sup(tmp_path, ORDINARY + TIMINGS)

        t = sup.last_task_timings()

        assert t["decode_tok_s"] == 32
        assert "draft_acceptance" not in t


class TestTheUsageLineSaysIt:
    """A fake supervisor shaped like this call site would agree with the call
    site even if the call site were missing, so both objects here are real."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from dpc_client_core.providers.llamacpp_server_provider import _ACTIVE_SUPERVISORS

        _ACTIVE_SUPERVISORS.clear()
        yield
        _ACTIVE_SUPERVISORS.clear()

    def _provider(self, tmp_path, contents):
        from dpc_client_core.providers import LlamaServerProvider

        provider = LlamaServerProvider(
            "local_qwen38", {"type": "llamacpp_server", "gguf_path": GGUF}
        )
        sup, _ = _sup(tmp_path, contents)
        provider.supervisor = sup
        return provider

    def _record(self, provider):
        from types import SimpleNamespace

        return provider._record_usage(
            SimpleNamespace(prompt_tokens=1621, completion_tokens=757, total_tokens=2378),
            path="tools", elapsed_s=27.0,
        )

    def test_a_drafted_turn_carries_the_numbers_and_says_them(self, tmp_path, caplog):
        provider = self._provider(tmp_path, ORDINARY + TIMINGS + DRAFT_N4)

        with caplog.at_level(
            logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"
        ):
            usage = self._record(provider)

        assert usage["speed"]["draft_n_max"] == 4
        assert usage["speed"]["draft_acceptance"] == pytest.approx(0.92647)
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "draft=92.6% at n=4" in line
        assert "4.71 tok/pass" in line

    def test_a_turn_with_no_speculation_says_nothing_about_it(self, tmp_path, caplog):
        provider = self._provider(tmp_path, ORDINARY + TIMINGS)

        with caplog.at_level(
            logging.INFO, logger="dpc_client_core.providers.llamacpp_server_provider"
        ):
            usage = self._record(provider)

        assert "draft_acceptance" not in usage["speed"]
        assert "draft=" not in "\n".join(r.getMessage() for r in caplog.records)
