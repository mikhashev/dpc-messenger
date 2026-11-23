"""The conversation cache's SSD tier is off unless the alias sets `conversation_spill_mib`.

Mike's call, 2026-11-23: opt-in. Since Strata v0.2.16 the engine spills the parked conversations its
RAM tier evicts to an SSD tier, on by default, in a folder beside the packs. DPC writes the tier itself:
`conversation_spill_mib` 0 or absent = `--conversation-spill-mib 0`; N > 0 = inside the alias's
`slot_save_path` (the KV snapshots folder), capped at N MiB; no folder or its disk missing = off.
The engine log is the only place a spill shows (the launcher's /metrics has no spill field at v0.2.20),
so the scanner reads it: an eviction that went to disk is not a loss and does not take a peer's guest entry.

The log lines are the engine's own format strings (generate.cpp, v0.2.20). No engine runs.
"""

import json
import logging
import os
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.managers import kv_snapshots as kv
from dpc_client_core.managers.llama_server_supervisor import LlamaServerError
from dpc_client_core.managers.strata_supervisor import (
    STRATA_DEFAULTS, StrataSupervisor, cache_loss_kind, conversation_spill_mib_error, strata_config_errors,
)
from dpc_client_core.providers.strata_provider import StrataProvider, _STRATA_FLAG_KEYS

from tests.test_strata_supervisor import BASE_ARGS, home, _install, _sup, _value  # noqa: F401

LOGGER = "dpc_client_core.managers.strata_supervisor"
PEER, OTHER = "dpc-node-guest-a", "dpc-node-guest-b"
P = "strata serve: conversation cache: "

SPILLED = P + ("spilled 5120 tokens to the SSD tier (evicted from RAM, written in the background) "
               "in 812.4 ms; on SSD 1, 1900 MiB\n")
SPILLED_AT_THE_FLOOR = P + ("spilled 5120 tokens to the SSD tier (the RAM floor after the capture) "
                            "in 90.0 ms; on SSD 1, 1900 MiB\n")
NOT_SPILLING = P + ("not spilling 800 tokens (evicted from RAM): reading them again (about 120 ms here) "
                    "costs less than the SSD round trip of 90 MiB (about 300 ms)\n")
NOT_SPILLING_AT_THE_FLOOR = P + ("not spilling 800 tokens (the RAM floor after the capture): reading them "
                                 "again (about 120 ms here) costs less than the SSD round trip of 90 MiB "
                                 "(about 300 ms)\n")
COULD_NOT_SPILL = P + ("could not spill 5120 tokens to the SSD tier (evicted from RAM, written in the "
                       "background): no space left on device\n")
COULD_NOT_SPILL_OOM = P + "could not spill to the SSD tier (evicted from RAM): out of memory\n"
COULD_NOT_SPILL_AT_THE_FLOOR = P + ("could not spill 5120 tokens to the SSD tier (the RAM floor after the "
                                    "capture): no space left on device\n")
READ_BACK = P + ("read 5120 tokens back from the SSD tier in 640.2 ms (this prompt starts with 5000 of "
                 "them); on SSD 0, 0 MiB\n")
COULD_NOT_READ = P + ("could not read 5120 tokens back from the SSD tier (the entry is gone); "
                      "the prompt is read again\n")
EVICTED_NO_LINE = P + "parked 2184 MiB evictions=3\n"  # the counter moves, the engine says nothing else
TIER_DIR = "E:/kv/conversation-spill/engine-1234"
TIER_ON = P + f"SSD tier in {TIER_DIR}, up to 32768 MiB\n"
TIER_ON_CLEANED = P + f"SSD tier in {TIER_DIR}, up to 512 MiB, 2 folders of ended engines removed\n"
TIER_OFF = P + "no SSD tier: the disk is full\n"


def _args(sup):
    return sup.build_start_config(5000)["args"]


def _last(args, flag):
    """What the engine takes: its parser assigns, so the last occurrence of a flag wins."""
    return [args[i + 1] for i, a in enumerate(args) if a == flag][-1]


def _say(caplog, sup, level=logging.INFO):
    with caplog.at_level(level, logger=LOGGER):
        sup.write_start_config(5000)
    return [r for r in caplog.records if "strata[flash next]" in r.getMessage()]


class TestTheFlags:
    @pytest.mark.parametrize("config", [{}, {"conversation_spill_mib": 0}, {"conversation_spill_mib": None}])
    def test_off_unless_the_alias_asks_even_with_a_kv_folder(self, tmp_path, home, config):
        args = _args(_sup(_install(tmp_path), slot_save_path=str(tmp_path / "kv"), **config))
        assert _value(args, "--conversation-spill-mib") == "0" and args.count("--conversation-spill-mib") == 1
        assert "--conversation-spill-dir" not in args

    def test_a_value_with_a_kv_folder_names_the_folder_inside_it_and_the_cap(self, tmp_path, home):
        folder = tmp_path / "kv"
        args = _args(_sup(_install(tmp_path), slot_save_path=str(folder), conversation_spill_mib=32768))
        assert args.count("--conversation-spill-dir") == 1 and args.count("--conversation-spill-mib") == 1
        assert _value(args, "--conversation-spill-dir") == str(folder / "conversation-spill")
        assert _value(args, "--conversation-spill-mib") == "32768"

    def test_the_cap_is_the_fields_not_the_kv_snapshot_cap(self, tmp_path, home):
        args = _args(_sup(_install(tmp_path), slot_save_path=str(tmp_path / "kv"),
                          conversation_spill_mib=1000, kv_snapshot_max_gib=1.5))
        assert _value(args, "--conversation-spill-mib") == "1000"

    @pytest.mark.parametrize("value", [None, ""])
    def test_a_value_without_a_kv_folder_is_off_and_names_no_folder(self, tmp_path, home, value):
        args = _args(_sup(_install(tmp_path), slot_save_path=value, conversation_spill_mib=4096))
        assert _value(args, "--conversation-spill-mib") == "0"
        assert "--conversation-spill-dir" not in args

    def test_the_default_is_zero(self):
        assert STRATA_DEFAULTS["conversation_spill_mib"] == 0

    def test_the_base_configs_own_spill_flags_are_replaced_not_kept(self, tmp_path, home):
        base = BASE_ARGS + ["--conversation-spill-dir", "D:/elsewhere", "--conversation-spill-mib", "100"]
        root = _install(tmp_path, args=base)
        on = _args(_sup(root, slot_save_path=str(tmp_path / "kv"), conversation_spill_mib=200))
        assert on.count("--conversation-spill-dir") == 1 and on.count("--conversation-spill-mib") == 1
        assert _value(on, "--conversation-spill-dir") == str(tmp_path / "kv" / "conversation-spill")
        assert _value(on, "--conversation-spill-mib") == "200"
        off = _args(_sup(root))
        assert "--conversation-spill-dir" not in off and off.count("--conversation-spill-mib") == 1
        assert _value(off, "--conversation-spill-mib") == "0"

    def test_the_aliass_extra_args_come_after_ours_and_win(self, tmp_path, home):
        extra = ["--conversation-spill-mib", "0", "--conversation-spill-dir", "D:/mine"]
        args = _args(_sup(_install(tmp_path), slot_save_path=str(tmp_path / "kv"),
                          conversation_spill_mib=4096, extra_args=extra))
        assert args[-4:] == extra
        assert _last(args, "--conversation-spill-mib") == "0"
        assert _last(args, "--conversation-spill-dir") == "D:/mine"

    def test_extra_args_win_where_the_field_is_off_too(self, tmp_path, home):
        args = _args(_sup(_install(tmp_path), extra_args=["--conversation-spill-mib", "2048"]))
        assert _last(args, "--conversation-spill-mib") == "2048"

    def test_building_the_config_creates_no_folder_for_the_engine(self, tmp_path, home):
        folder = tmp_path / "kv"
        sup = _sup(_install(tmp_path), slot_save_path=str(folder), conversation_spill_mib=4096)
        sup.build_start_config(5000)
        sup.write_start_config(5000)
        assert not (folder / "conversation-spill").exists()

    def test_a_missing_disk_turns_the_tier_off_with_the_layer(self, tmp_path, home):
        sup = _sup(_install(tmp_path), slot_save_path=str(tmp_path / "usb-drive" / "kv"),
                   conversation_spill_mib=4096)
        written = json.loads(sup.write_start_config(5000).read_text(encoding="utf-8"))
        assert written["slot_save_path"] == ""
        assert _last(written["args"], "--conversation-spill-mib") == "0"
        assert not (tmp_path / "usb-drive").exists()

    def test_a_missing_disk_leaves_an_extra_args_value_the_last(self, tmp_path, home):
        sup = _sup(_install(tmp_path), slot_save_path=str(tmp_path / "usb-drive" / "kv"),
                   conversation_spill_mib=4096, extra_args=["--conversation-spill-mib", "512"])
        written = json.loads(sup.write_start_config(5000).read_text(encoding="utf-8"))
        assert _last(written["args"], "--conversation-spill-mib") == "512"


class TestTheField:
    @pytest.mark.parametrize("value", [None, 0, 1, 32768])
    def test_a_non_negative_integer_or_nothing_is_accepted(self, value):
        assert conversation_spill_mib_error(value) is None

    @pytest.mark.parametrize("bad", [-1, 1.5, 2.0, "4096", True, False, [1]])
    def test_anything_else_is_refused_at_build_and_at_save(self, tmp_path, home, bad):
        with pytest.raises(LlamaServerError, match="conversation_spill_mib"):
            _sup(_install(tmp_path), conversation_spill_mib=bad).build_start_config(5000)
        config = {"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                  "conversation_cache_mib": 1, "conversation_spill_mib": bad}
        errors = strata_config_errors(config)
        assert errors == [f"conversation_spill_mib: expected a whole number, at least 0 (0 is off), not {bad!r}"]

    def test_an_edit_restarts_the_launcher_and_an_explicit_zero_does_not(self, tmp_path):
        cfg = {"strata_install_dir": str(_install(tmp_path)), "strata_base_config": "strata-base.json",
               "conversation_cache_mib": 1024}
        sig = StrataProvider._flag_signature
        assert "conversation_spill_mib" in _STRATA_FLAG_KEYS
        assert sig(cfg) != sig({**cfg, "conversation_spill_mib": 4096})
        assert sig({**cfg, "conversation_spill_mib": 4096}) != sig({**cfg, "conversation_spill_mib": 8192})
        assert sig(cfg) == sig({**cfg, "conversation_spill_mib": 0})

    def test_the_snapshot_cap_is_still_not_a_restart_key(self):
        assert "kv_snapshot_max_gib" not in _STRATA_FLAG_KEYS  # an edit applies at the next start


class TestWhatTheStartSays:
    def test_a_value_without_a_folder_says_why_the_tier_is_off_at_info(self, tmp_path, home, caplog):
        records = _say(caplog, _sup(_install(tmp_path), conversation_spill_mib=4096))
        [rec] = [r for r in records if "SSD tier is off" in r.getMessage()]
        assert rec.levelno == logging.INFO
        assert rec.getMessage() == ("strata[flash next]: conversation_spill_mib is 4096 MiB but the SSD tier "
                                    "is off: slot_save_path is empty and the SSD tier lives inside that folder")

    def test_a_missing_disk_says_so_at_info(self, tmp_path, home, caplog):
        folder = tmp_path / "usb-drive" / "kv"
        records = _say(caplog, _sup(_install(tmp_path), slot_save_path=str(folder), conversation_spill_mib=4096))
        [rec] = [r for r in records if "SSD tier is off" in r.getMessage()]
        assert rec.levelno == logging.INFO and "is missing at this start" in rec.getMessage()
        assert "KV snapshots layer is off" in rec.getMessage()

    @pytest.mark.parametrize("on_with_folder", [False, True])
    def test_nothing_is_said_when_the_field_is_off_or_the_tier_is_on(self, tmp_path, home, caplog, on_with_folder):
        config = ({"conversation_spill_mib": 4096, "slot_save_path": str(tmp_path / "kv")}
                  if on_with_folder else {"conversation_spill_mib": 0})
        records = _say(caplog, _sup(_install(tmp_path), **config))
        assert not [r for r in records if "SSD tier" in r.getMessage() or "conversation_spill" in r.getMessage()]

    @pytest.mark.parametrize("extra, flags", [
        (["--conversation-spill-mib", "0"], ["--conversation-spill-mib"]),
        (["--conversation-spill-dir", "D:/x"], ["--conversation-spill-dir"]),
        (["--conversation-spill-mib", "0", "--conversation-spill-dir", "D:/x"],
         ["--conversation-spill-mib", "--conversation-spill-dir"]),
    ])
    def test_extra_args_naming_the_flag_warn_but_are_not_refused(self, tmp_path, home, caplog, extra, flags):
        sup = _sup(_install(tmp_path), extra_args=extra)
        records = _say(caplog, sup, logging.WARNING)
        warned = [r for r in records if r.levelno == logging.WARNING]
        assert len(warned) == len(flags)
        for flag, rec in zip(flags, warned):
            assert rec.getMessage() == (
                f"strata[flash next]: extra_args carries {flag}, which overrides the alias field "
                "conversation_spill_mib; that field is the place for it")
        assert strata_config_errors({"strata_install_dir": "Z:/none", "strata_base_config": "x.json",
                                     "conversation_cache_mib": 1, "extra_args": extra}) == []

    def test_other_extra_args_do_not_warn(self, tmp_path, home, caplog):
        records = _say(caplog, _sup(_install(tmp_path), extra_args=["--spec", "4"]), logging.WARNING)
        assert not [r for r in records if r.levelno == logging.WARNING]


class TestTheEnginesOwnStartLine:
    def test_it_is_read_for_its_folder_and_cap_at_info(self, tmp_path, caplog):
        sup = _engine(tmp_path, conversation_spill_mib=32768)
        _append(sup, TIER_ON)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_ssd_tier()
        [rec] = [r for r in caplog.records if "SSD tier" in r.getMessage()]
        assert rec.levelno == logging.INFO
        assert rec.getMessage() == f"strata[flash]: conversation cache: SSD tier in {TIER_DIR}, up to 32768 MiB"

    def test_the_cleaned_folders_suffix_does_not_enter_the_folder(self, tmp_path, caplog):
        sup = _engine(tmp_path, conversation_spill_mib=512)
        _append(sup, TIER_ON_CLEANED)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_ssd_tier()
        [rec] = [r for r in caplog.records if "SSD tier" in r.getMessage()]
        assert rec.getMessage().endswith(f"{TIER_DIR}, up to 512 MiB")

    def test_no_tier_while_the_field_asks_is_a_warning(self, tmp_path, caplog):
        sup = _engine(tmp_path, conversation_spill_mib=32768)
        _append(sup, TIER_OFF)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_ssd_tier()
        [rec] = [r for r in caplog.records if "SSD tier" in r.getMessage()]
        assert rec.levelno == logging.WARNING
        assert rec.getMessage() == ("strata[flash]: conversation_spill_mib is 32768 MiB but the engine started "
                                    "with no SSD tier: the disk is full")

    @pytest.mark.parametrize("config", [{}, {"conversation_spill_mib": 0}])
    def test_no_tier_while_the_field_is_zero_is_info(self, tmp_path, caplog, config):
        sup = _engine(tmp_path, **config)
        _append(sup, TIER_OFF)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_ssd_tier()
        [rec] = [r for r in caplog.records if "SSD tier" in r.getMessage()]
        assert rec.levelno == logging.INFO
        assert rec.getMessage() == "strata[flash]: conversation cache: no SSD tier: the disk is full"

    def test_it_is_said_once_and_a_silent_engine_says_nothing(self, tmp_path, caplog):
        sup = _engine(tmp_path, conversation_spill_mib=4096)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_ssd_tier()
            assert not [r for r in caplog.records if "SSD tier" in r.getMessage()]
            _append(sup, TIER_ON)
            sup.log_ssd_tier()
            sup.log_ssd_tier()
            sup.log_restore_refusals()
        assert len([r for r in caplog.records if "conversation cache: SSD tier in" in r.getMessage()]) == 1

    def test_the_first_call_after_the_start_says_it_too(self, tmp_path, caplog):
        sup = _engine(tmp_path, conversation_spill_mib=4096)
        _append(sup, TIER_ON)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_restore_refusals()
        assert len([r for r in caplog.records if "conversation cache: SSD tier in" in r.getMessage()]) == 1

    def test_a_new_launch_reads_its_own_line(self, tmp_path):
        sup = _engine(tmp_path, conversation_spill_mib=4096)
        _append(sup, TIER_ON)
        sup._scan_engine_log()
        sup.prime_restore_scan()
        assert sup._ssd_tier_unsaid is None

    def test_the_start_lines_are_no_loss_and_no_spill(self):
        assert cache_loss_kind(TIER_ON) is None and cache_loss_kind(TIER_OFF) is None


class TestWhichLinesAreLosses:
    @pytest.mark.parametrize("line, kind", [
        (NOT_SPILLING, "not spilling"),
        (COULD_NOT_SPILL, "could not spill"),
        (COULD_NOT_SPILL_OOM, "could not spill"),
        (COULD_NOT_READ, "could not read back"),
    ])
    def test_a_refused_spill_and_a_failed_read_are_losses_with_their_own_word(self, line, kind):
        assert cache_loss_kind(line) == kind

    @pytest.mark.parametrize("line", [
        SPILLED, SPILLED_AT_THE_FLOOR, READ_BACK, EVICTED_NO_LINE,
        # the same refusals at the RAM floor are followed by `skip parking (RAM floor ...)`, which counts
        NOT_SPILLING_AT_THE_FLOOR, COULD_NOT_SPILL_AT_THE_FLOOR,
    ])
    def test_a_spill_a_read_back_and_the_floors_own_lines_are_not(self, line):
        assert cache_loss_kind(line) is None


def _engine(tmp_path, polled=0, **config) -> StrataSupervisor:
    sup = StrataSupervisor("flash", {
        "strata_install_dir": str(tmp_path), "strata_base_config": "strata-base.json",
        "conversation_cache_mib": 10240, **config,
    })
    sup._engine_log_path = tmp_path / "strata-flash-engine.log"
    sup._engine_log_path.write_text("", encoding="utf-8")
    sup._get = AsyncMock()
    sup.prime_restore_scan()
    sup._cache_evictions_polled = polled
    return sup


def _append(sup, text):
    with open(sup._engine_log_path, "a", encoding="utf-8") as f:
        f.write(text)


def _said(caplog):
    return [r.getMessage() for r in caplog.records if "strata[flash]" in r.getMessage()]


class TestASpillIsNotALoss:
    def test_spills_are_taken_off_the_eviction_count_and_said_as_spills(self, tmp_path, caplog):
        sup = _engine(tmp_path)
        _append(sup, SPILLED + SPILLED)
        sup._cache_evictions_polled = 2
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert sup.log_restore_refusals() == 0
        text = " ".join(_said(caplog))
        assert "2 evicted conversation(s) spilled to the SSD tier" in text and "not lost" in text
        assert "loss" not in text and "eviction counter" not in text

    def test_an_eviction_with_no_spill_line_is_still_counted(self, tmp_path, caplog):
        sup = _engine(tmp_path)
        _append(sup, SPILLED)
        sup._cache_evictions_polled = 3
        assert sup._new_cache_evictions() == 2

    def test_the_counter_only_line_shows_what_was_netted_too(self, tmp_path, caplog):
        sup = _engine(tmp_path)
        _append(sup, SPILLED)
        sup._cache_evictions_polled = 3
        with caplog.at_level(logging.INFO, logger=LOGGER):
            sup.log_restore_refusals()
        text = " ".join(_said(caplog))
        assert "engine eviction counter +2 since the last call" in text
        assert text.endswith("(evictions=+3 spilled=+1 lost=+2)")

    def test_a_spill_at_the_ram_floor_is_no_eviction_and_nets_nothing(self, tmp_path):
        sup = _engine(tmp_path)
        _append(sup, SPILLED_AT_THE_FLOOR)
        sup._cache_evictions_polled = 1
        assert sup._new_cache_evictions() == 1

    def test_a_refused_spill_and_a_failed_spill_are_losses_by_their_word(self, tmp_path, caplog):
        sup = _engine(tmp_path)
        _append(sup, NOT_SPILLING + COULD_NOT_SPILL + SPILLED)
        sup._cache_evictions_polled = 3
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert sup.log_restore_refusals() == 2
        text = " ".join(_said(caplog))
        assert "2 conversation-cache loss(es)" in text and "1 not spilling" in text and "1 could not spill" in text
        assert "engine eviction counter +2" in text

    def test_a_read_back_is_said_at_info_and_is_no_loss(self, tmp_path, caplog):
        sup = _engine(tmp_path)
        _append(sup, READ_BACK)
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert sup.log_restore_refusals() == 0
        records = [r for r in caplog.records if "read back from the SSD tier" in r.getMessage()]
        assert len(records) == 1 and records[0].levelno == logging.INFO
        assert "read 5120 tokens back" in records[0].getMessage()

    def test_a_failed_read_back_is_a_loss(self, tmp_path):
        sup = _engine(tmp_path)
        _append(sup, COULD_NOT_READ)
        assert sup.log_restore_refusals() == 1

    def test_a_new_launch_forgets_the_spills_seen(self, tmp_path):
        sup = _engine(tmp_path)
        _append(sup, SPILLED)
        sup._cache_evictions_polled = 1
        sup._scan_engine_log()
        sup.prime_restore_scan()
        sup._cache_evictions_polled = 1
        assert sup._new_cache_evictions() == 1


class TestASpillDoesNotTakeThePeersEntry:
    def _held(self, tmp_path, polled=7):
        sup = _engine(tmp_path, polled=polled)
        sup.note_guest_call(PEER, 125000, True)
        assert sup._guest_entry_holder() == PEER
        return sup

    def test_an_eviction_the_log_shows_as_spilled_keeps_the_entry(self, tmp_path):
        sup = self._held(tmp_path)
        _append(sup, SPILLED)
        sup._cache_evictions_polled = 8
        assert sup._guest_entry_holder() == PEER

    def test_a_plain_eviction_drops_it(self, tmp_path):
        sup = self._held(tmp_path)
        _append(sup, EVICTED_NO_LINE)
        sup._cache_evictions_polled = 8
        assert sup._guest_entry_holder() is None

    def test_a_count_that_rose_by_more_than_the_spills_drops_it(self, tmp_path):
        sup = self._held(tmp_path)
        _append(sup, SPILLED)
        sup._cache_evictions_polled = 9
        assert sup._guest_entry_holder() is None

    def test_a_refused_spill_drops_it(self, tmp_path):
        sup = self._held(tmp_path)
        _append(sup, NOT_SPILLING)
        sup._cache_evictions_polled = 8
        assert sup._guest_entry_holder() is None

    def test_a_spill_from_before_the_entry_was_taken_hides_no_later_eviction(self, tmp_path):
        sup = _engine(tmp_path, polled=7)
        _append(sup, SPILLED)
        sup.note_guest_call(PEER, 125000, True)
        _append(sup, EVICTED_NO_LINE)
        sup._cache_evictions_polled = 8
        assert sup._guest_entry_holder() is None

    def test_a_smaller_count_is_a_new_engine_and_drops_it(self, tmp_path):
        sup = self._held(tmp_path)
        sup._cache_evictions_polled = 2
        assert sup._guest_entry_holder() is None

    @pytest.mark.asyncio
    async def test_the_idle_limit_still_covers_a_silent_drop_from_the_ssd_tier(self, tmp_path):
        sup = self._held(tmp_path)
        _append(sup, SPILLED)
        sup._cache_evictions_polled = 8
        sup._guest_entry["at"] -= 901.0
        assert sup._guest_entry_holder() is None


class TestOurSweepLeavesStratasFolderAlone:
    """`<slot_save_path>/conversation-spill/<per-process folder>/...` is Strata's: made and removed by
    the engine inside the KV snapshots folder. The sweep lists the folder by name, deletes only DPC's
    names and counts only what the index names."""

    def _spill_tree(self, folder: Path) -> list:
        spill = folder / "conversation-spill"
        mine, dead = spill / "engine-1234", spill / "engine-99"
        mine.mkdir(parents=True)
        dead.mkdir()
        files = [mine / "entry-0001.spill", dead / "dpc-kv-abc123.bin", spill / "dpc-kv-abc124.staging"]
        for f in files:
            f.write_bytes(b"s" * 64)
        old = time.time() - 10 ** 6
        for f in files:
            os.utime(f, (old, old))
        return files

    def test_a_sweep_deletes_nothing_in_the_subfolder_and_counts_none_of_it(self, tmp_path):
        kv.reset_state()
        files = self._spill_tree(tmp_path)
        _, final = kv.new_snapshot_names(tmp_path)
        (tmp_path / final).write_bytes(b"k" * 16)
        entry = kv.new_entry(file=final, alias="flash", engine="strata", identity={"engine_version": "0.1.66"},
                             record={}, n_saved=10, nbytes=16, save_ms=1, now=time.time(), ttl_s=3600)
        assert kv.add_entry(tmp_path, entry, cap_bytes=1000)
        # a cap the index's one entry fits and the subfolder's 192 bytes would not
        out = kv.sweep(tmp_path, alias="flash", now=time.time(), expected={"engine_version": "0.1.66"},
                       cap_bytes=100)
        assert out == []
        assert all(f.exists() for f in files) and (tmp_path / final).exists()
        assert [e["file"] for e in kv.read_index(tmp_path)["entries"]] == [final]

    def test_a_folder_that_holds_only_the_subfolder_is_swept_and_prepared_without_error(self, tmp_path):
        kv.reset_state()
        files = self._spill_tree(tmp_path)
        assert kv.prepare_folder(tmp_path) is None
        assert kv.sweep(tmp_path, alias="flash", now=time.time(), cap_bytes=1) == []
        assert all(f.exists() for f in files)

    def test_a_per_process_folder_removed_between_two_sweeps_is_no_trouble(self, tmp_path):
        kv.reset_state()
        files = self._spill_tree(tmp_path)
        assert kv.sweep(tmp_path, alias="flash", now=time.time()) == []
        shutil.rmtree(tmp_path / "conversation-spill" / "engine-1234")
        assert kv.sweep(tmp_path, alias="flash", now=time.time()) == []
        assert files[1].exists() and files[2].exists()


class TestTheLaunchSaysIt:
    @pytest.mark.asyncio
    async def test_a_real_launch_reads_the_engines_start_line_before_any_call(self, tmp_path, home, monkeypatch, caplog):
        from tests.test_strata_supervisor import _started_through_the_real_launch
        sup = _sup(_install(tmp_path), slot_save_path=str(tmp_path / "kv"), conversation_spill_mib=4096)
        spawned = _started_through_the_real_launch(sup, monkeypatch, metrics={"engine": {"version": "0.1.66"}})
        real_spawn = sup._spawn

        async def spawn_and_print(cmd, env):
            sup._engine_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(sup._engine_log_path, "a", encoding="utf-8") as f:
                f.write(TIER_OFF)
            return await real_spawn(cmd, env)

        sup._spawn = spawn_and_print
        with caplog.at_level(logging.INFO, logger=LOGGER):
            await sup.ensure_running()
        warned = [r for r in caplog.records if r.levelno == logging.WARNING and "no SSD tier" in r.getMessage()]
        assert len(warned) == 1 and "conversation_spill_mib is 4096 MiB" in warned[0].getMessage()
        assert len(spawned) == 1
        await sup.stop()
