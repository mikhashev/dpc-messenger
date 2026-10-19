"""A conversation the Strata cache gave up is said from the engine log.

The provider asks the supervisor for `log_restore_refusals` after every call.
The base reads the launcher's stdout for llama.cpp's `failed to find N available
cells`; for Strata that file never holds such a line, and the cache's own
events are in the engine log. The lines below are copied from this machine's
Strata engine log, not written to fit the parser.
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers.strata_supervisor import StrataSupervisor, cache_loss_kind

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


# One line per printf format the engine's park and restore paths hold in
# src/program/generate.cpp, sample values filled in, with the class each is named by.
_E = "strata serve: conversation cache: "
LOSSES = [
    (_E + "skip parking (3120 tokens, below the 8192 minimum; parked=2)", "below min tokens"),
    (_E + "skip parking (stage CUDA1: no free device memory)", "stage CUDA failed"),
    (_E + "skip parking (unsupported layer kind)", "snapshot size failed"),
    (_E + "skip parking (snapshot 6144 MiB exceeds available budget)", "over budget"),
    (_E + "skip parking (physical RAM admission; need 4096 MiB plus 2560 MiB floor, "
          "available 3100 MiB; evicted 2, 1 still parked)", "RAM admission"),
    (_E + "evicted 2 parked conversations to admit this snapshot (4096 MiB plus 2560 MiB floor)", "evicted"),
    (_E + "evicted 1 parked conversation to admit this snapshot (2184 MiB plus 2560 MiB floor)", "evicted"),
    (_E + "skip parking (RAM floor after capture: 2560 MiB needed, available 1900 MiB)", "RAM floor"),
    (_E + "skipped 45184 tokens in 812.5 ms; parked=2 bytes=3709866004 evictions=3 "
          "snapshot_bytes=2290000000 reused_kv_bytes=0", "not stored"),
    (_E + "allocation failed; skip parking", "allocation failed"),
    (_E + "discard snapshot (stage count)", "discarded at restore"),
    (_E + "discard invalid stage snapshot (geometry differs)", "discarded at restore"),
    (_E + "discard invalid snapshot (geometry differs)", "discarded at restore"),
    (_E + "cannot borrow from a parked conversation (gone)", "borrow refused"),
    (_E + "no room beside the parked conversation this request starts with; "
          "taking it instead of borrowing", "borrow refused"),
    (_E + "the state at 4096 tokens has this prompt's tokens but picture 2 (at token 812) is not the "
          "same as when it was saved (its embeddings changed): it cannot be reused (#2228)", "picture changed"),
]
WORKING = [
    _E + "parked 45184 tokens in 812.5 ms; parked=3 bytes=3709866004 evictions=0 "
         "snapshot_bytes=2290000000 reused_kv_bytes=1000",
    _E + "restored 45184 tokens (checkpoint) in 173.4 ms; parked=2 bytes=3709866004",
    _E + "borrowed 45184 tokens from a parked conversation, which stays parked, in 173.4 ms; "
         "parked=2 bytes=3709866004 borrowed=1",
    _E + "dropped 2 superseded copies of this conversation; parked=1",
    _E + "dropped 1 superseded copy of this conversation; parked=1",
]


class TestEveryLossTheEngineNamesIsCountedWithItsClass:
    @pytest.mark.parametrize("line,kind", LOSSES)
    def test_a_loss_line_has_its_class(self, line, kind):
        assert cache_loss_kind(line) == kind

    @pytest.mark.parametrize("line", WORKING)
    def test_a_cache_working_line_is_not_a_loss(self, line):
        assert cache_loss_kind(line) is None

    def test_each_line_is_counted_and_the_record_names_the_classes(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        _append(sup._engine_log_path, "".join(f"{line}\n" for line in WORKING + [l for l, _ in LOSSES]))

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            said = sup.log_restore_refusals()

        assert said == len(LOSSES)
        text = [r.getMessage() for r in caplog.records if "conversation-cache" in r.getMessage()][0]
        assert f"{len(LOSSES)} conversation-cache loss" in text
        for kind in {kind for _, kind in LOSSES}:
            assert kind in text
        assert "1 snapshot size failed" in text and "1 stage CUDA failed" in text and "3 discarded at restore" in text

    def test_working_lines_alone_say_nothing(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        _append(sup._engine_log_path, "".join(f"{line}\n" for line in WORKING))

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            assert sup.log_restore_refusals() == 0

        assert not [r for r in caplog.records if "conversation-cache" in r.getMessage()]


class TestTheLaunchersEvictionCounterRidesBesideTheLines:
    """/metrics conversation_cache.evictions counts every eviction the engine made,
    the slot and budget ones too, which have no log line of their own."""

    def _polled(self, sup, evictions):
        sup._note_cache_evictions({"conversation_cache": {"evictions": evictions}})

    def test_the_delta_since_the_last_record_is_in_the_same_record(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        self._polled(sup, 4)
        _append(sup._engine_log_path, EVICTED)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            assert sup.log_restore_refusals() == 1
            self._polled(sup, 6)
            _append(sup._engine_log_path, SKIPPED)
            assert sup.log_restore_refusals() == 1

        texts = [r.getMessage() for r in caplog.records if "conversation-cache" in r.getMessage()]
        assert len(texts) == 2
        assert "engine eviction counter +4" in texts[0]
        assert "engine eviction counter +2" in texts[1]

    def test_evictions_without_a_loss_line_are_said_alone_and_not_counted_as_losses(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        self._polled(sup, 3)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            assert sup.log_restore_refusals() == 0
            assert sup.log_restore_refusals() == 0

        texts = [r.getMessage() for r in caplog.records if "eviction counter" in r.getMessage()]
        assert len(texts) == 1 and "+3" in texts[0]

    def test_a_restarted_engine_counts_from_zero_again(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        self._polled(sup, 9)
        sup.log_restore_refusals()
        sup.prime_restore_scan()  # a new launch
        self._polled(sup, 1)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            sup.log_restore_refusals()

        assert "eviction counter +1 " in caplog.records[-1].getMessage()

    def test_a_counter_that_drops_without_a_restart_is_rebased_and_said(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        self._polled(sup, 9)
        sup.log_restore_refusals()
        self._polled(sup, 2)  # no prime_restore_scan: the engine's counter started over

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            sup.log_restore_refusals()

        texts = [r.getMessage() for r in caplog.records]
        rebase = [t for t in texts if "started over" in t]
        assert len(rebase) == 1
        assert sup.alias in rebase[0] and "from 9 to 2" in rebase[0]
        assert any("eviction counter +2 " in t for t in texts)

    def test_a_counter_that_only_grows_says_nothing_about_starting_over(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        self._polled(sup, 3)
        sup.log_restore_refusals()
        self._polled(sup, 5)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            sup.log_restore_refusals()

        assert not [r for r in caplog.records if "started over" in r.getMessage()]

    def test_a_metrics_body_without_the_counter_changes_nothing(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup.prime_restore_scan()
        for body in (None, {}, {"conversation_cache": None}, {"conversation_cache": {"evictions": "x"}},
                     {"conversation_cache": {"evictions": True}}):
            sup._note_cache_evictions(body)

        with caplog.at_level(logging.INFO, logger="dpc_client_core.managers.strata_supervisor"):
            assert sup.log_restore_refusals() == 0

        assert not caplog.records

    @pytest.mark.asyncio
    async def test_the_watcher_poll_feeds_the_counter(self, tmp_path, monkeypatch):
        sup = _sup(tmp_path)
        sup.config["engine_watch_interval_s"] = 0.01
        sup.prime_restore_scan()

        class _Proc:
            returncode = None

        polls = []

        async def fake_get(path):
            polls.append(path)
            return {"live": {"state": "idle"}, "conversation_cache": {"evictions": 5}}

        monkeypatch.setattr(sup, "_get", fake_get)
        monkeypatch.setattr(sup, "_sample_free_commit", lambda: None)
        sup._proc = _Proc()
        sup._start_watch()
        try:
            while not polls:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.02)
        finally:
            sup._watch_task.cancel()
        assert sup._cache_evictions_polled == 5
