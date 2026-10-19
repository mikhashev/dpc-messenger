"""A conversation the Strata cache gave up is said from the engine log.

The provider asks the supervisor for `log_restore_refusals` after every call.
The base reads the launcher's stdout for llama.cpp's `failed to find N available
cells`; for Strata that file never holds such a line, and the cache's own
events are in the engine log. The lines below are copied from this machine's
Strata engine log, not written to fit the parser.
"""

import logging

from dpc_client_core.managers.strata_supervisor import StrataSupervisor

EVICTED = (
    "conversation cache: evicted 1 parked conversation to admit this snapshot "
    "(2184 MiB plus 2560 MiB floor)\n"
)
SKIPPED = "conversation cache: skip parking (snapshot 6144 MiB exceeds available budget)\n"
NOISE = (
    "borrowed 45184 tokens from a parked conversation, which stays parked, in 173.4 ms; "
    "parked=2 bytes=3709866004 borrowed=1\n"
    "conversation cache: dropped 1 superseded copy\n"
    "conversation cache: parked 2184 MiB evictions=0\n"
)


def _sup(tmp_path, contents=""):
    sup = StrataSupervisor("flash next", {
        "strata_install_dir": str(tmp_path),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240,
    })
    sup._log_path = tmp_path / "strata-flash_next.log"
    sup._log_path.write_text("launcher: listening\n", encoding="utf-8")
    sup._engine_log_path = tmp_path / "strata-flash_next-engine.log"
    sup._engine_log_path.write_text(contents, encoding="utf-8")
    return sup


def _append(log, text):
    with open(log, "a", encoding="utf-8") as f:
        f.write(text)


class TestTheEngineLogIsTheOneRead:
    def test_an_eviction_and_a_skipped_parking_are_counted_and_quoted(self, tmp_path, caplog):
        sup = _sup(tmp_path, NOISE)
        sup.prime_restore_scan()
        _append(sup._engine_log_path, NOISE + EVICTED + NOISE + SKIPPED + NOISE)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            said = sup.log_restore_refusals()

        assert said == 2
        records = [r for r in caplog.records if "strata[flash next]" in r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        text = records[0].getMessage()
        assert "2 conversation-cache loss" in text
        assert EVICTED.strip() in text and SKIPPED.strip() in text
        assert "borrowed" not in text and "superseded" not in text

    def test_a_second_call_with_nothing_new_says_nothing(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        _append(sup._engine_log_path, EVICTED + SKIPPED)
        assert sup.log_restore_refusals() == 2

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            assert sup.log_restore_refusals() == 0
            _append(sup._engine_log_path, NOISE)
            assert sup.log_restore_refusals() == 0

        assert not [r for r in caplog.records if "conversation-cache" in r.getMessage()]

    def test_what_the_previous_child_wrote_is_not_reported(self, tmp_path):
        sup = _sup(tmp_path, EVICTED + SKIPPED)
        sup.prime_restore_scan()

        assert sup.log_restore_refusals() == 0

    def test_the_launcher_log_is_not_read(self, tmp_path):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        _append(sup._log_path, EVICTED + SKIPPED)

        assert sup.log_restore_refusals() == 0

    def test_the_first_call_without_a_prime_primes_and_reports_nothing(self, tmp_path):
        sup = _sup(tmp_path, EVICTED)

        assert sup.log_restore_refusals() == 0
        _append(sup._engine_log_path, SKIPPED)
        assert sup.log_restore_refusals() == 1


class TestTheFileMovesUnderTheCursor:
    def test_a_missing_engine_log_is_zero_and_the_cursor_survives(self, tmp_path):
        sup = _sup(tmp_path, EVICTED)
        sup.prime_restore_scan()
        sup._engine_log_path.unlink()

        assert sup.log_restore_refusals() == 0

        sup._engine_log_path.write_text(EVICTED + SKIPPED, encoding="utf-8")
        assert sup.log_restore_refusals() == 1  # the part past the old cursor

    def test_a_truncated_log_is_read_from_its_first_byte(self, tmp_path):
        sup = _sup(tmp_path, NOISE * 20)
        sup.prime_restore_scan()
        sup._engine_log_path.write_text(SKIPPED, encoding="utf-8")

        assert sup.log_restore_refusals() == 1

    def test_a_line_still_being_written_is_read_whole_next_time(self, tmp_path):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        half = len("conversation cache: evicted 1 parked")
        _append(sup._engine_log_path, EVICTED[:half])
        assert sup.log_restore_refusals() == 0

        _append(sup._engine_log_path, EVICTED[half:])
        assert sup.log_restore_refusals() == 1

    def test_many_events_are_counted_but_only_a_few_quoted(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        _append(sup._engine_log_path, EVICTED * 7)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            assert sup.log_restore_refusals() == 7

        text = [r.getMessage() for r in caplog.records if "conversation-cache" in r.getMessage()][0]
        assert "7 conversation-cache loss" in text
        assert text.count("evicted 1 parked conversation") == 3
        assert "+4 more" in text
