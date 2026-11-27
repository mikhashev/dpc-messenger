"""The privacy collection switches and the log levels: saved from the form, refused
when a value is bad (THE-PRIVACY-COLLECTION-AND-LOGGING-SETTINGS-HAVE-NO-CONTROL-
IN-THE-UI).

What device information is collected and how the node logs could be set only in
config.ini; the audit (audit/config_ui_parity_2026-11-27.md) lists the two [system]
collection switches and every [logging] level key as config-only, and the dead keys
(collect_hardware_specs, collect_dev_tools) as the dead-key card's. The privacy and
logging form now covers the live keys through one command pair whose writer validates
every value before writing anything. What is asserted:

- the two new commands are allowed and are CoreService methods, delegated to
  PrivacyLoggingService;
- a save of the form's payload lands in config.ini under [system], [logging] and
  [logging.modules], and the getters the readers read answer the new values;
- an invalid level is refused with a reason naming its key and writes nothing, not
  even the valid neighbouring key sent in the same command; the neighbour alone
  still passes;
- a key left out keeps its stored value, and a module left out of module_levels
  keeps its stored level;
- no live log path exists — the readers apply the levels once at backend start
  (run_service.setup_logging) — so a save changes config.ini and not the process's
  loggers, which is what the form's restart note says.

Every home is under tmp_path; `DPC_HOME` points there, never at `~/.dpc`. No
collection runs, no logger is reconfigured.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS
from dpc_client_core.privacy_logging_service import PrivacyLoggingService
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings

# The form's payload: the two collection switches and the three level shapes, named
# as config.ini names them (the command writes what it is given, verbatim).
PRIVACY_LOGGING_FORM = {
    "auto_collect_device_info": False,
    "collect_ai_models": True,
    "level": "DEBUG",
    "console_level": "WARNING",
    "module_levels": {"httpx": "DEBUG", "my.module": "ERROR"},
}


class RecordingApi:
    """A local API that records the events a save broadcasts."""

    def __init__(self):
        self.events = []

    async def broadcast_event(self, event_name, payload):
        self.events.append((event_name, payload))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A DPC home of its own, so no test reads or writes ~/.dpc."""
    monkeypatch.setenv("DPC_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def host(home):
    """A CoreService-shaped host whose privacy/logging service is real, with a
    recording API."""
    api = RecordingApi()
    service = PrivacyLoggingService(
        settings=Settings(home),
        local_api=api,
    )
    return SimpleNamespace(privacy_logging_service=service)


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_privacy_logging_config", "save_privacy_logging_config"):
            assert name in ALLOWED_COMMANDS
            assert callable(getattr(CoreService, name, None))

    @pytest.mark.asyncio
    async def test_the_get_command_answers_the_keys_with_their_defaults(self, home, host):
        config = await CoreService.get_privacy_logging_config(host)
        assert config["status"] == "success"
        assert config["auto_collect_device_info"] is True
        assert config["collect_ai_models"] is False
        assert config["level"] == "INFO"
        assert config["console_level"] == "INFO"
        # The seeded third-party overrides, as the section holds them.
        assert config["module_levels"]["httpx"] == "WARNING"
        assert config["module_levels"]["openai"] == "WARNING"
        assert config["module_levels"]["anthropic"] == "WARNING"


class TestAPrivacyLoggingSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_privacy_logging_config(
            host, config=dict(PRIVACY_LOGGING_FORM))
        assert out == {"status": "success",
                       "message": "Privacy and logging settings saved successfully"}
        file = Settings(home)
        assert file.get("system", "auto_collect_device_info") == "false"
        assert file.get("system", "collect_ai_models") == "true"
        assert file.get("logging", "level") == "DEBUG"
        assert file.get("logging", "console_level") == "WARNING"
        assert file.get("logging.modules", "httpx") == "DEBUG"
        assert file.get("logging.modules", "my.module") == "ERROR"

    @pytest.mark.asyncio
    async def test_the_readers_getters_answer_the_new_value(self, home, host):
        await CoreService.save_privacy_logging_config(
            host, config=dict(PRIVACY_LOGGING_FORM))
        file = Settings(home)
        # The switches are read at each device-context collection (service start
        # and device_context_collector read them through these parses); the levels
        # are read once at backend start (run_service.setup_logging) through these.
        assert file.get_auto_collect_device_info() is False
        assert file.get_privacy_logging_settings()["collect_ai_models"] is True
        assert file.get_log_level() == "DEBUG"
        assert file.get_log_console_level() == "WARNING"
        assert file.get_module_log_levels()["httpx"] == "DEBUG"

    @pytest.mark.asyncio
    async def test_a_level_is_taken_in_any_case_and_stored_as_the_reader_reads_it(
            self, home, host):
        out = await CoreService.save_privacy_logging_config(
            host, config={"level": "debug", "console_level": " Critical "})
        assert out["status"] == "success"
        file = Settings(home)
        # The getters upper-case what they read, so any case answers the reader.
        assert file.get_log_level() == "DEBUG"
        assert file.get_log_console_level() == "CRITICAL"

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await CoreService.save_privacy_logging_config(
            host, config={"auto_collect_device_info": False})
        file = Settings(home)
        assert file.get("system", "auto_collect_device_info") == "false"
        assert file.get("system", "collect_ai_models") == "false", "untouched"
        assert file.get("logging", "level") == "INFO", "untouched"
        assert file.get("logging", "console_level") == "INFO", "untouched"

    @pytest.mark.asyncio
    async def test_a_module_left_out_of_module_levels_keeps_its_stored_level(
            self, home, host):
        await CoreService.save_privacy_logging_config(
            host, config={"module_levels": {"httpx": "DEBUG"}})
        file = Settings(home)
        assert file.get("logging.modules", "httpx") == "DEBUG"
        assert file.get("logging.modules", "openai") == "WARNING", "untouched"

    @pytest.mark.asyncio
    async def test_an_invalid_level_is_refused_and_the_valid_neighbour_still_passes(
            self, home, host):
        Settings(home)  # the seeds are written here, before the snapshot
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_privacy_logging_config(
            host, config={"level": "BOGUS", "console_level": "DEBUG"})
        assert out["status"] == "error" and "level" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring key was not written either"
        # The neighbour alone passes, and the refused key still holds its stored value.
        out = await CoreService.save_privacy_logging_config(
            host, config={"console_level": "DEBUG"})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("logging", "console_level") == "DEBUG"
        assert file.get_log_level() == "INFO", "the refused value"

    @pytest.mark.asyncio
    async def test_an_invalid_module_level_is_refused_and_writes_nothing(
            self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_privacy_logging_config(
            host, config={"module_levels": {"httpx": "VERBOSE"}})
        assert out["status"] == "error"
        assert "module_levels[httpx]" in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"auto_collect_device_info": "yes"}, "auto_collect_device_info"),
        ({"auto_collect_device_info": 1}, "auto_collect_device_info"),
        ({"collect_ai_models": "true"}, "collect_ai_models"),
        ({"collect_ai_models": 0}, "collect_ai_models"),
        ({"level": "TRACE"}, "level"),
        ({"level": ""}, "level"),
        ({"level": 10}, "level"),
        ({"level": True}, "level"),
        ({"console_level": "NOTSET"}, "console_level"),  # not a level this build knows
        ({"module_levels": {"": "DEBUG"}}, "module_levels"),
        ({"module_levels": {"bad name": "DEBUG"}}, "module_levels"),
        ({"module_levels": {"a..b": "DEBUG"}}, "module_levels"),
        ({"module_levels": {"httpx": 7}}, "module_levels"),
        ({"module_levels": ["httpx"]}, "module_levels"),
    ])
    async def test_values_of_the_wrong_kind_are_refused_with_their_key(
            self, home, host, config, key):
        out = await CoreService.save_privacy_logging_config(host, config=config)
        assert out["status"] == "error" and key in out["message"]

    @pytest.mark.asyncio
    async def test_an_unknown_key_is_refused_including_the_dead_system_ones(
            self, home, host):
        out = await CoreService.save_privacy_logging_config(
            host, config={"collect_hardware_specs": False})
        assert out["status"] == "error" and "collect_hardware_specs" in out["message"]
        out = await CoreService.save_privacy_logging_config(
            host, config={"max_bytes": "20"})
        assert out["status"] == "error" and "max_bytes" in out["message"]
        assert not (home / "agents").exists(), "nothing but config.ini was touched"

    @pytest.mark.asyncio
    async def test_no_live_log_path_exists_so_a_save_leaves_the_process_loggers_alone(
            self, home, host):
        """The levels are applied by run_service.setup_logging once at start; no
        setter reconfigures logging live. A save must therefore change config.ini
        and not the process's loggers — the restart note in the form says this,
        and this pins that no half-applied path was invented."""
        root_before = logging.getLogger().level
        httpx_before = logging.getLogger("httpx").level
        out = await CoreService.save_privacy_logging_config(
            host, config={"level": "DEBUG", "module_levels": {"httpx": "DEBUG"}})
        assert out["status"] == "success"
        assert Settings(home).get_log_level() == "DEBUG"
        assert logging.getLogger().level == root_before, "the root logger was not touched"
        assert logging.getLogger("httpx").level == httpx_before, "nor the module logger"

    @pytest.mark.asyncio
    async def test_a_save_broadcasts_the_stored_values(self, home, host):
        await CoreService.save_privacy_logging_config(
            host, config={"collect_ai_models": True})
        (name, payload) = host.privacy_logging_service.local_api.events[-1]
        assert name == "privacy_logging_config_updated"
        assert payload["collect_ai_models"] is True
        assert payload["auto_collect_device_info"] is True
        assert payload["level"] == "INFO"
