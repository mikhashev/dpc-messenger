"""The conversation cache's occupancy is recorded from every watcher poll and
said at INFO when it changes (Johnny's condition (a) on variant C, 2026-11-12:
record before deciding).

The watcher already read `/metrics` every `engine_watch_interval_s` and kept
only `evictions`. It now keeps the whole `conversation_cache` view, and one
INFO line is written when `parked`, `slots`, `bytes` or `budget_mib` differ
from the last line, so a day's distribution can be read from the log. The
bodies below carry the keys Strata v0.2.11's launcher answers with
(serve/server.py `ConvCacheLog.reset` and `conversation_cache_view`).
"""

import asyncio
import logging

import pytest

from dpc_client_core.managers import strata_supervisor as strata_mod
from dpc_client_core.managers.strata_supervisor import StrataSupervisor

LOGGER = "dpc_client_core.managers.strata_supervisor"


def _sup(tmp_path):
    sup = StrataSupervisor("flash", {
        "strata_install_dir": str(tmp_path),
        "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240,
    })
    sup._engine_log_path = tmp_path / "strata-flash-engine.log"
    sup._engine_log_path.write_text("", encoding="utf-8")
    return sup


def _body(parked=1, slots=2, held=1 << 30, budget=10240, evictions=0):
    return {"conversation_cache": {
        "enabled": True, "budget_mib": budget, "slots": slots, "parked": parked, "bytes": held,
        "evictions": evictions, "parks": 3, "restores": 1, "last_event": "parked",
        "last_tokens": 64988, "last_at": 1.0, "requests": 4,
    }}


def _said(caplog):
    return [r for r in caplog.records
            if r.name == LOGGER and "conversation cache parked=" in r.getMessage()]


@pytest.fixture(autouse=True)
def _ram(monkeypatch):
    monkeypatch.setattr(strata_mod, "read_free_commit_or_physical",
                        lambda: {"avail_phys_mib": 40484, "total_phys_mib": 63168})


class TestOneLinePerChange:
    def test_the_first_reading_is_said_with_its_numbers(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup._note_cache_view(_body(parked=2, slots=2, held=5233293096, budget=10240, evictions=7))
        (line,) = _said(caplog)
        text = line.getMessage()
        assert line.levelno == logging.INFO
        assert "strata[flash]" in text
        assert "parked=2 of slots=2" in text
        assert "4990 MiB of budget_mib=10240" in text
        assert "evictions=7" in text
        assert "free physical RAM 40484 MiB" in text
        assert sup.cache_view["parked"] == 2 and sup.cache_view["bytes"] == 5233293096
        assert isinstance(sup.cache_view["at"], float)

    def test_the_same_numbers_again_say_nothing_and_still_refresh_the_view(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup._note_cache_view(_body())
        first_at = sup.cache_view["at"]
        with caplog.at_level(logging.INFO, logger=LOGGER):
            for _ in range(5):
                # Counters that are not the four do not make a line of their own.
                sup._note_cache_view(_body(evictions=3))
        assert _said(caplog) == []
        assert sup.cache_view["evictions"] == 3
        assert sup.cache_view["at"] >= first_at

    @pytest.mark.parametrize("change", [
        {"parked": 2}, {"slots": 3}, {"held": 2 << 30}, {"budget": 12288},
    ])
    def test_each_of_the_four_changing_is_a_line(self, tmp_path, caplog, change):
        sup = _sup(tmp_path)
        sup._note_cache_view(_body())
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup._note_cache_view(_body(**change))
            sup._note_cache_view(_body(**change))
        assert len(_said(caplog)) == 1

    def test_a_body_without_the_cache_keeps_no_view_and_says_nothing(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            for body in (None, {}, {"conversation_cache": None}, {"conversation_cache": "x"}):
                sup._note_cache_view(body)
        assert sup.cache_view is None
        assert _said(caplog) == []

    def test_a_new_launch_says_its_first_reading_even_when_equal(self, tmp_path, caplog):
        sup = _sup(tmp_path)
        sup._note_cache_view(_body())
        sup.prime_restore_scan()
        assert sup.cache_view is None
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup._note_cache_view(_body())
        assert len(_said(caplog)) == 1

    def test_unknown_values_are_named_not_guessed(self, tmp_path, caplog, monkeypatch):
        monkeypatch.setattr(strata_mod, "read_free_commit_or_physical", lambda: None)
        sup = _sup(tmp_path)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup._note_cache_view({"conversation_cache": {"parked": 0, "slots": None, "bytes": True}})
        (line,) = _said(caplog)
        text = line.getMessage()
        assert "slots=unknown" in text and "unknown MiB" in text and "free physical RAM unread" in text


class TestTheWatcherRecords:
    @pytest.mark.asyncio
    async def test_polls_with_a_change_between_them_give_two_lines(self, tmp_path, monkeypatch, caplog):
        sup = _sup(tmp_path)
        sup.config["engine_watch_interval_s"] = 0.01
        sup.prime_restore_scan()
        bodies = [_body(parked=1)] * 3 + [_body(parked=2)] * 3
        polls = []

        async def fake_get(path):
            polls.append(path)
            body = bodies[min(len(polls), len(bodies)) - 1]
            return {"live": {"state": "idle"}, **body}

        class _Proc:
            returncode = None

        monkeypatch.setattr(sup, "_get", fake_get)
        monkeypatch.setattr(sup, "_sample_free_commit", lambda: None)
        sup._proc = _Proc()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup._start_watch()
            try:
                while len(polls) < len(bodies) + 1:
                    await asyncio.sleep(0.01)
            finally:
                sup._watch_task.cancel()
        assert set(polls) == {"/metrics"}
        lines = [r.getMessage() for r in _said(caplog)]
        assert len(lines) == 2
        assert "parked=1 of slots=2" in lines[0] and "parked=2 of slots=2" in lines[1]
        assert sup.cache_view["parked"] == 2
