"""Tests for configurable `[music] default_engine`, installed-engine fallback, licence metadata,
and the `set_music_default_engine` local API command.
"""

from __future__ import annotations

import asyncio
import configparser
import logging
from pathlib import Path

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.music import commands, job_schemas as js
from dpc_client_core.music.capabilities import EngineStatus, MusicCapabilities
from dpc_client_core.music.commands import MUSIC_COMMANDS, MusicCommandsMixin
from dpc_client_core.settings import Settings
from tests._acestep_helpers import FakeAceStepServer, free_env, install_fake_engine
from tests._music_job_commands_helpers import without_tools
from tests._yue2_helpers import FakeYue2Server, install_both, install_fake_yue2, yue2_env


class _RealSettingsService(MusicCommandsMixin):
    def __init__(self, home: Path):
        self.settings = Settings(home)


def _service_at(home: Path, monkeypatch) -> _RealSettingsService:
    without_tools(monkeypatch)
    monkeypatch.setattr(commands, "AceStepServer", FakeAceStepServer)
    monkeypatch.setattr(commands, "Yue2Server", FakeYue2Server)
    svc = _RealSettingsService(home)
    svc.settings.set("music", "acestep_port", "0")
    svc.settings.set("music", "yue2_port", "0")
    return svc


# -- settings.py -----------------------------------------------------------------


def test_shipped_default_is_yue2_in_fresh_and_old_configs(tmp_path):
    fresh_home = tmp_path / "fresh"
    fresh_home.mkdir()
    fresh = Settings(fresh_home)
    assert fresh.get_music_default_engine() == "yue2"
    parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    parser.read(fresh_home / "config.ini", encoding="utf-8")
    assert parser["music"]["default_engine"] == "yue2"

    old_home = tmp_path / "old"
    old_home.mkdir()
    (old_home / "config.ini").write_text("[hub]\nurl = http://localhost:8000\n", encoding="utf-8")
    old = Settings(old_home)
    assert old.get_music_default_engine() == "yue2"
    old_parser = configparser.ConfigParser(inline_comment_prefixes=("#",))
    old_parser.read(old_home / "config.ini", encoding="utf-8")
    assert old_parser["music"]["default_engine"] == "yue2"


def test_setting_and_validating_default_engine_in_settings(tmp_path, caplog):
    home = tmp_path / "dpc"
    home.mkdir()
    s = Settings(home)

    assert s.set_music_default_engine("acestep") == "acestep"
    assert s.get_music_default_engine() == "acestep"
    assert Settings(home).get_music_default_engine() == "acestep"

    with pytest.raises(ValueError, match="default_engine"):
        s.set_music_default_engine("suno")
    assert s.get_music_default_engine() == "acestep"

    # Hand-edited invalid config falls back to yue2 with a warning.
    s.set("music", "default_engine", "bogus-engine")
    with caplog.at_level(logging.WARNING, logger="dpc_client_core.settings"):
        assert s.get_music_default_engine() == "yue2"
    assert any("default_engine" in r.getMessage() for r in caplog.records)


# -- MusicCapabilities: fallback & licences --------------------------------------


def test_capabilities_expose_licence_metadata_and_configured_default():
    installed = {
        "yue2": lambda: EngineStatus(state="stopped", reason="installed", key="yue2"),
        "acestep": lambda: EngineStatus(state="stopped", reason="installed", key="acestep"),
    }
    caps = MusicCapabilities(engines=installed, default_engine="acestep")
    report = caps.report()
    assert report["configured_default_engine"] == "acestep"
    assert report["default_engine"] == "acestep"
    assert report["default_engine_fallback_reason"] is None
    assert report["engine"]["key"] == "acestep"
    assert report["limits"]["generate_from_text"]["engine"] == "acestep"
    assert report["limits"]["generate_from_text"]["duration_s"] == {"min": 10, "max": 600, "default": 120}
    assert report["features"]["cover"]["default_engine"] == "acestep"
    assert report["features"]["same_style"]["default_engine"] == "acestep"

    ace_info = report["engines"]["acestep"]
    yue_info = report["engines"]["yue2"]
    assert ace_info["default"] is True and ace_info["configured_default"] is True
    assert yue_info["default"] is False and yue_info["configured_default"] is False
    assert ace_info["licence_key"] == "mit" and ace_info["non_commercial"] is False
    assert "MIT" in ace_info["licence_summary"] and "commercial" in ace_info["licence_summary"].lower()
    assert "cc-by-nc-4.0" in yue_info["licence_key"] and yue_info["non_commercial"] is True
    assert "CC BY-NC 4.0" in yue_info["licence_summary"] and "non-commercial" in yue_info["licence_summary"].lower()


def test_uninstalled_configured_default_falls_back_to_installed_engine_with_reason():
    only_ace = {
        "yue2": lambda: EngineStatus(state="not_installed", reason="yue-server binary is missing", key="yue2"),
        "acestep": lambda: EngineStatus(state="stopped", reason="installed", key="acestep"),
    }
    report = MusicCapabilities(engines=only_ace, default_engine="yue2").report()
    assert report["configured_default_engine"] == "yue2"
    assert report["default_engine"] == "acestep"
    assert report["engine"]["key"] == "acestep"
    assert report["engines"]["acestep"]["default"] is True
    assert report["engines"]["yue2"]["default"] is False
    assert report["engines"]["yue2"]["configured_default"] is True
    reason = report["default_engine_fallback_reason"]
    assert reason and "yue2" in reason and "acestep" in reason and "yue-server binary is missing" in reason

    only_yue = {
        "yue2": lambda: EngineStatus(state="stopped", reason="installed", key="yue2"),
        "acestep": lambda: EngineStatus(state="not_installed", reason="ace-server missing", key="acestep"),
    }
    report2 = MusicCapabilities(engines=only_yue, default_engine="acestep").report()
    assert report2["configured_default_engine"] == "acestep"
    assert report2["default_engine"] == "yue2"
    assert report2["default_engine_fallback_reason"]
    assert "acestep" in report2["default_engine_fallback_reason"] and "yue2" in report2["default_engine_fallback_reason"]

    neither = {
        "yue2": lambda: EngineStatus(state="not_installed", reason="yue missing", key="yue2"),
        "acestep": lambda: EngineStatus(state="not_installed", reason="ace missing", key="acestep"),
    }
    report3 = MusicCapabilities(engines=neither, default_engine="acestep").report()
    assert report3["configured_default_engine"] == "acestep"
    assert report3["default_engine"] == "acestep"
    assert report3["default_engine_fallback_reason"] is None


# -- commands.py & local_api.py --------------------------------------------------


def test_set_music_default_engine_is_registered_on_door():
    assert "set_music_default_engine" in MUSIC_COMMANDS
    assert "set_music_default_engine" in ALLOWED_COMMANDS
    assert asyncio.iscoroutinefunction(getattr(MusicCommandsMixin, "set_music_default_engine"))


@pytest.mark.asyncio
async def test_set_music_default_engine_updates_capabilities_and_omitted_job_engine(tmp_path, monkeypatch):
    free_env(monkeypatch)
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    svc = _service_at(home, monkeypatch)
    try:
        # Default starts at yue2.
        caps0 = (await svc.get_music_capabilities())["capabilities"]
        assert caps0["default_engine"] == "yue2"
        assert caps0["configured_default_engine"] == "yue2"

        # Invalid value returns mapped error and leaves config untouched.
        bad = await svc.set_music_default_engine(engine="suno")
        assert bad["status"] == "error" and bad["code"] == "invalid_params" and bad["field"] == "engine"
        assert svc.settings.get_music_default_engine() == "yue2"

        # Switch default to acestep.
        res = await svc.set_music_default_engine(engine="acestep")
        assert res["status"] == "ok"
        assert res["configured_default_engine"] == "acestep"
        assert res["default_engine"] == "acestep"
        assert res["default_engine_fallback_reason"] is None
        assert res["capabilities"]["default_engine"] == "acestep"
        assert svc.settings.get_music_default_engine() == "acestep"

        status = await svc.get_music_engine_status()
        assert status["default_engine"] == "acestep" and status["engine"]["key"] == "acestep"

        # check_music_job and propose_music_job without explicit engine use acestep.
        checked = await svc.check_music_job(
            type="generate_from_text",
            params={"lyrics": "[verse]\nline\n", "style": "indie rock"},
        )
        assert checked["status"] == "ok"
        assert checked["params"]["engine"] == "acestep"
        assert checked["params"]["duration_s"] == js.DURATION_DEFAULT_S

        proposed = await svc.propose_music_job(
            type="generate_from_text",
            params={"lyrics": "[verse]\nline\n", "style": "indie rock"},
        )
        assert proposed["status"] == "ok"
        assert proposed["job"]["state"] == "queued"
        assert proposed["job"]["params"]["engine"] == "acestep"
        assert proposed["job"]["params"]["duration_s"] == js.DURATION_DEFAULT_S
    finally:
        await svc.stop_music_jobs()
        await svc.stop_music_library()


@pytest.mark.asyncio
async def test_commands_fall_back_to_installed_engine_and_surface_reason_when_configured_is_missing(tmp_path, monkeypatch):
    free_env(monkeypatch)
    home = tmp_path / "dpc"
    install_fake_engine(home)  # acestep only; yue2 is not installed
    svc = _service_at(home, monkeypatch)
    try:
        # Configured default is shipped yue2, which is not installed.
        caps = (await svc.get_music_capabilities())["capabilities"]
        assert caps["configured_default_engine"] == "yue2"
        assert caps["default_engine"] == "acestep"
        assert caps["default_engine_fallback_reason"]
        assert "yue2" in caps["default_engine_fallback_reason"]

        checked = await svc.check_music_job(
            type="generate_from_text",
            params={"lyrics": "[verse]\nline\n", "style": "ambient"},
        )
        assert checked["status"] == "ok"
        assert checked["params"]["engine"] == "acestep"
        assert checked.get("default_engine_fallback_reason")

        proposed = await svc.propose_music_job(
            type="generate_from_text",
            params={"lyrics": "[verse]\nline\n", "style": "ambient"},
        )
        assert proposed["status"] == "ok"
        assert proposed["job"]["state"] == "queued"
        assert proposed["job"]["params"]["engine"] == "acestep"
        assert proposed.get("default_engine_fallback_reason")
    finally:
        await svc.stop_music_jobs()
        await svc.stop_music_library()


@pytest.mark.asyncio
async def test_start_music_jobs_uses_configured_default_engine_from_config_ini(tmp_path, monkeypatch):
    free_env(monkeypatch)
    yue2_env(monkeypatch)
    home = tmp_path / "dpc"
    install_both(home)
    s = Settings(home)
    s.set_music_default_engine("acestep")

    svc = _service_at(home, monkeypatch)
    try:
        await svc.start_music_jobs()
        caps = (await svc.get_music_capabilities())["capabilities"]
        assert caps["configured_default_engine"] == "acestep"
        assert caps["default_engine"] == "acestep"
        assert svc._music_engine_set().default == "acestep"
    finally:
        await svc.stop_music_jobs()
        await svc.stop_music_library()
