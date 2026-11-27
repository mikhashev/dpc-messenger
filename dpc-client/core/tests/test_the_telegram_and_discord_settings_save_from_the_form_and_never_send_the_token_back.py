"""The Telegram and Discord settings: saved from the form, refused when a value
is bad, and a stored token is never sent back as text
(THE-TELEGRAM-AND-DISCORD-SETTINGS-CAN-ONLY-BE-TYPED-INTO-CONFIG-INI).

A person with the UI could not enable Telegram or Discord or give them a token
without editing config.ini; the audit (audit/config_ui_parity_2026-11-27.md)
lists every [telegram] and [discord] key as config-only, and the dead
[telegram] history keys (fetch_history_on_startup, history_*,
drop_pending_updates) as the dead-key card's. The Telegram and Discord form
now covers the enable switches, the two bot tokens and the live
[discord.guardrails]/[discord.conversations] limits through one command pair
whose writer validates every value before writing anything. What is asserted:

- the two new commands are allowed and are CoreService methods, delegated to
  TelegramDiscordService;
- a save of the form's payload lands in config.ini under [telegram],
  [discord], [discord.guardrails] and [discord.conversations], and the getters
  the readers read answer the new values;
- a stored token is answered as `token_set` only — the token string is absent
  from the whole serialized get reply, the save reply, the broadcast event and
  every caplog record;
- an empty token field keeps the stored token (never an accidental empty
  overwrite); only the explicit `clear_bot_token` removes it, and a token set
  and cleared in one save is refused;
- an invalid value is refused with a reason naming its key and writes nothing,
  not even the valid neighbouring key sent in the same command; the neighbour
  alone still passes;
- the dead [telegram] keys and the keys this form does not own (guild, webhook,
  agent wiring, bot_token_env) are refused as unknown.

Nothing here invents a live path the code does not have: the switches and the
tokens are read once, when the backend builds the Telegram service
(telegram_service.py) and starts the Discord one (discord_service.py) — the
form says so per key — and the limits are read per message by
discord_coordinator.py, which needs no restart. Every home is under tmp_path;
`DPC_HOME` points there, never at `~/.dpc`. No bot is started. Every token in
this file is an obviously fake test value.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from dpc_client_core.local_api import ALLOWED_COMMANDS, _sanitize_payload_for_logging
from dpc_client_core.service import CoreService
from dpc_client_core.settings import Settings
from dpc_client_core.telegram_discord_service import TelegramDiscordService

# Obviously fake, the shape of nothing a real bot would accept.
TG = "TEST-TOKEN-NOT-REAL"
DC = "TEST-DISCORD-TOKEN-NOT-REAL"

# The form's payload: the two sections as the command receives them.
TELEGRAM_DISCORD_FORM = {
    "telegram": {"enabled": True, "bot_token": TG},
    "discord": {
        "enabled": True,
        "bot_token": DC,
        "rate_limit_per_user_max": 5,
        "rate_limit_per_user_window": 120,
        "rate_limit_global_max": 30,
        "rate_limit_global_window": 600,
        "response_delay_seconds": 0,
        "ttl_minutes": 15,
        "max_messages": 20,
    },
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
    """A CoreService-shaped host whose Telegram/Discord service is real, with
    a recording API."""
    api = RecordingApi()
    service = TelegramDiscordService(
        settings=Settings(home),
        local_api=api,
    )
    return SimpleNamespace(telegram_discord_service=service)


class TestTheCommands:
    def test_the_new_commands_are_allowed_and_are_service_methods(self):
        for name in ("get_telegram_discord_config", "save_telegram_discord_config"):
            assert name in ALLOWED_COMMANDS
            assert callable(getattr(CoreService, name, None))

    @pytest.mark.asyncio
    async def test_the_get_command_answers_the_keys_with_their_defaults(self, home, host):
        config = await CoreService.get_telegram_discord_config(host)
        assert config["status"] == "success"
        assert config["telegram"] == {"enabled": False, "token_set": False}
        assert config["discord"]["enabled"] is False
        assert config["discord"]["token_set"] is False
        assert config["discord"]["bot_token_env"] == ""
        # The reader's own fallbacks (discord_coordinator.py).
        assert config["discord"]["rate_limit_per_user_max"] == 10
        assert config["discord"]["rate_limit_per_user_window"] == 600
        assert config["discord"]["rate_limit_global_max"] == 60
        assert config["discord"]["rate_limit_global_window"] == 3600
        assert config["discord"]["response_delay_seconds"] == 3
        assert config["discord"]["ttl_minutes"] == 30
        assert config["discord"]["max_messages"] == 50


class TestTheTokenIsAnsweredAsPresenceOnly:
    @pytest.mark.asyncio
    async def test_a_stored_token_is_never_in_the_get_reply(self, home, host):
        await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"bot_token": TG},
                          "discord": {"bot_token": DC}})
        reply = await CoreService.get_telegram_discord_config(host)
        assert reply["telegram"]["token_set"] is True
        assert reply["discord"]["token_set"] is True
        # Not the value, not a prefix, not a length: the whole serialized
        # reply, at any depth, must not contain the token strings.
        text = json.dumps(reply)
        assert TG not in text and DC not in text
        assert TG[:9] not in text and DC[:9] not in text
        # The section answers exactly presence, not a value-shaped hole.
        assert set(reply["telegram"]) == {"enabled", "token_set"}

    @pytest.mark.asyncio
    async def test_the_save_reply_and_the_broadcast_carry_no_token_text(
            self, home, host):
        out = await CoreService.save_telegram_discord_config(
            host, config=dict(TELEGRAM_DISCORD_FORM))
        assert out["status"] == "success"
        assert TG not in json.dumps(out) and DC not in json.dumps(out)
        (name, payload) = host.telegram_discord_service.local_api.events[-1]
        assert name == "telegram_discord_config_updated"
        text = json.dumps(payload)
        assert TG not in text and DC not in text
        assert payload["telegram"]["token_set"] is True
        assert payload["discord"]["token_set"] is True
        assert payload["discord"]["rate_limit_per_user_max"] == 5

    @pytest.mark.asyncio
    async def test_no_log_record_written_by_a_save_carries_the_token(
            self, home, host, caplog):
        with caplog.at_level(logging.DEBUG, logger="dpc_client_core"):
            out = await CoreService.save_telegram_discord_config(
                host, config=dict(TELEGRAM_DISCORD_FORM))
            assert out["status"] == "success"
            # A refused save too: its reason names the key, never the value.
            refused = await CoreService.save_telegram_discord_config(
                host, config={"telegram": {"bot_token": 123}})
            assert refused["status"] == "error"
        assert TG not in caplog.text and DC not in caplog.text

    def test_the_typed_token_is_redacted_in_the_local_api_payload_log(self):
        """The one place a token travels is the save command itself, and
        local_api logs every UI command's payload at DEBUG
        (test_no_secret_reaches_a_log_file.py pins that layer); this pins that
        the exact shape this form sends — config.<section>.bot_token — is one
        the redactor catches by field name, whatever the token's shape."""
        sanitized = _sanitize_payload_for_logging({
            "config": {"telegram": {"enabled": True, "bot_token": TG},
                       "discord": {"bot_token": DC, "ttl_minutes": 15}}})
        text = json.dumps(sanitized)
        assert TG not in text and DC not in text
        assert sanitized["config"]["telegram"]["bot_token"] == "<redacted>"
        assert sanitized["config"]["discord"]["bot_token"] == "<redacted>"
        assert sanitized["config"]["discord"]["ttl_minutes"] == 15


class TestATelegramDiscordSave:
    @pytest.mark.asyncio
    async def test_the_forms_payload_lands_in_config_ini(self, home, host):
        out = await CoreService.save_telegram_discord_config(
            host, config=dict(TELEGRAM_DISCORD_FORM))
        assert out == {"status": "success",
                       "message": "Telegram and Discord settings saved successfully"}
        file = Settings(home)
        assert file.get("telegram", "enabled") == "true"
        assert file.get("telegram", "bot_token") == TG
        assert file.get("discord", "enabled") == "true"
        assert file.get("discord", "bot_token") == DC
        assert file.get("discord.guardrails", "rate_limit_per_user_max") == "5"
        assert file.get("discord.guardrails", "rate_limit_per_user_window") == "120"
        assert file.get("discord.guardrails", "rate_limit_global_max") == "30"
        assert file.get("discord.guardrails", "rate_limit_global_window") == "600"
        # 0 is an honest "answer at once", not a refusal.
        assert file.get("discord.guardrails", "response_delay_seconds") == "0"
        assert file.get("discord.conversations", "ttl_minutes") == "15"
        assert file.get("discord.conversations", "max_messages") == "20"

    @pytest.mark.asyncio
    async def test_the_readers_getters_answer_the_new_values(self, home, host):
        await CoreService.save_telegram_discord_config(
            host, config=dict(TELEGRAM_DISCORD_FORM))
        file = Settings(home)
        # The switches and tokens are read once, at service construction
        # (telegram_service.py) and at Discord start (discord_service.py),
        # through these getters; the limits are read per message by
        # discord_coordinator.py through the same raw keys.
        assert file.get_telegram_enabled() is True
        assert file.get_telegram_bot_token() == TG
        assert file.get_discord_enabled() is True
        assert file.get_discord_bot_token() == DC
        assert int(file.get("discord.guardrails", "rate_limit_per_user_max")) == 5
        assert int(file.get("discord.conversations", "ttl_minutes")) == 15

    @pytest.mark.asyncio
    async def test_an_empty_token_field_keeps_the_stored_token(self, home, host):
        await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"bot_token": TG},
                          "discord": {"bot_token": DC}})
        # A save without the key, and a save with an explicit empty string —
        # the two ways a left-empty password field can arrive — both keep it.
        out = await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"enabled": True},
                          "discord": {"bot_token": ""}})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get_telegram_bot_token() == TG
        assert file.get_discord_bot_token() == DC
        reply = await CoreService.get_telegram_discord_config(host)
        assert reply["telegram"]["token_set"] is True
        assert reply["discord"]["token_set"] is True

    @pytest.mark.asyncio
    async def test_the_explicit_clear_removes_the_stored_token(self, home, host):
        await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"bot_token": TG},
                          "discord": {"bot_token": DC}})
        out = await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"clear_bot_token": True},
                          "discord": {"clear_bot_token": True}})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get_telegram_bot_token() == ""
        assert file.get_discord_bot_token() == ""
        reply = await CoreService.get_telegram_discord_config(host)
        assert reply["telegram"]["token_set"] is False
        assert reply["discord"]["token_set"] is False

    @pytest.mark.asyncio
    async def test_a_token_set_and_cleared_in_one_save_is_refused(self, home, host):
        Settings(home)  # the seeds are written here, before the snapshot
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_telegram_discord_config(
            host, config={"discord": {"bot_token": DC, "clear_bot_token": True}})
        assert out["status"] == "error" and "discord.bot_token" in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_a_key_left_out_keeps_its_stored_value(self, home, host):
        await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"enabled": True}})
        file = Settings(home)
        assert file.get("telegram", "enabled") == "true"
        assert file.get("telegram", "bot_token") == "", "untouched"
        assert file.get("discord", "enabled") == "false", "untouched"
        assert file.get("discord.guardrails", "rate_limit_per_user_max") == "10", "untouched"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, key", [
        ({"telegram": {"enabled": "yes"}}, "telegram.enabled"),
        ({"telegram": {"enabled": 1}}, "telegram.enabled"),
        ({"telegram": {"bot_token": 123}}, "telegram.bot_token"),
        ({"discord": {"enabled": "true"}}, "discord.enabled"),
        ({"discord": {"enabled": 0}}, "discord.enabled"),
        ({"discord": {"bot_token": ["x"]}}, "discord.bot_token"),
        ({"discord": {"clear_bot_token": "yes"}}, "discord.clear_bot_token"),
        ({"discord": {"rate_limit_per_user_max": 0}}, "discord.guardrails.rate_limit_per_user_max"),
        ({"discord": {"rate_limit_per_user_max": "10"}}, "discord.guardrails.rate_limit_per_user_max"),
        ({"discord": {"rate_limit_per_user_max": 2.5}}, "discord.guardrails.rate_limit_per_user_max"),
        ({"discord": {"rate_limit_per_user_max": True}}, "discord.guardrails.rate_limit_per_user_max"),
        ({"discord": {"rate_limit_per_user_window": 0}}, "discord.guardrails.rate_limit_per_user_window"),
        ({"discord": {"rate_limit_global_max": -1}}, "discord.guardrails.rate_limit_global_max"),
        ({"discord": {"rate_limit_global_window": False}}, "discord.guardrails.rate_limit_global_window"),
        ({"discord": {"response_delay_seconds": -1}}, "discord.guardrails.response_delay_seconds"),
        ({"discord": {"response_delay_seconds": 1.5}}, "discord.guardrails.response_delay_seconds"),
        ({"discord": {"ttl_minutes": 0}}, "discord.conversations.ttl_minutes"),
        ({"discord": {"max_messages": "50"}}, "discord.conversations.max_messages"),
    ])
    async def test_an_invalid_value_is_refused_with_its_key_and_writes_nothing(
            self, home, host, config, key):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_telegram_discord_config(host, config=config)
        assert out["status"] == "error" and key in out["message"], out
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    async def test_a_valid_neighbour_is_not_written_on_refusal_then_passes_alone(
            self, home, host):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"enabled": True, "bot_token": 123},
                          "discord": {"rate_limit_per_user_max": 0,
                                      "ttl_minutes": 90}})
        assert out["status"] == "error" and "telegram.bot_token" in out["message"]
        assert (home / "config.ini").read_bytes() == before, \
            "the valid neighbouring keys were not written either"
        file = Settings(home)
        assert file.get("telegram", "enabled") == "false", "the refused payload's neighbour"
        assert file.get("discord.conversations", "ttl_minutes") == "30", "ditto"
        # Each neighbour alone passes, and the refused key still holds its stored value.
        out = await CoreService.save_telegram_discord_config(
            host, config={"telegram": {"enabled": True},
                          "discord": {"ttl_minutes": 90}})
        assert out["status"] == "success"
        file = Settings(home)
        assert file.get("telegram", "enabled") == "true"
        assert file.get("discord.conversations", "ttl_minutes") == "90"
        assert file.get("discord.guardrails", "rate_limit_per_user_max") == "10", "the refused value"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dead_key", [
        "fetch_history_on_startup",
        "history_fetch_limit",
        "history_max_age_hours",
        "history_message_types",
        "drop_pending_updates",
    ])
    async def test_a_dead_telegram_key_is_refused_as_unknown(
            self, home, host, dead_key):
        Settings(home)
        before = (home / "config.ini").read_bytes()
        out = await CoreService.save_telegram_discord_config(
            host, config={"telegram": {dead_key: False}})
        assert out["status"] == "error" and dead_key in out["message"]
        assert (home / "config.ini").read_bytes() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config, named", [
        ({"telegram": {"allowed_chat_ids": "[1]"}}, "allowed_chat_ids"),
        ({"telegram": {"use_webhook": True}}, "use_webhook"),
        ({"discord": {"guild_id": "123"}}, "guild_id"),
        ({"discord": {"bot_token_env": "DISCORD_BOT_TOKEN"}}, "bot_token_env"),
        ({"discord": {"agent_id": "agent_001"}}, "agent_id"),
        ({"discord": {"url_whitelist": "example.com"}}, "url_whitelist"),
        ({"voice": {"enabled": True}}, "voice"),
        ({"telegram": "on"}, "telegram"),
    ])
    async def test_a_key_the_form_does_not_own_is_refused_as_unknown(
            self, home, host, config, named):
        out = await CoreService.save_telegram_discord_config(host, config=config)
        assert out["status"] == "error" and named in out["message"]
        assert not (home / "agents").exists(), "nothing but config.ini was touched"

    @pytest.mark.asyncio
    async def test_the_get_answers_the_env_variable_name_the_discord_reader_prefers(
            self, home, host):
        host.telegram_discord_service.settings.set(
            "discord", "bot_token_env", "DISCORD_BOT_TOKEN")
        reply = await CoreService.get_telegram_discord_config(host)
        # The name, not a value: the reader prefers that variable when it is
        # set, and the form says so beside the token field.
        assert reply["discord"]["bot_token_env"] == "DISCORD_BOT_TOKEN"
        assert reply["discord"]["token_set"] is False, "nothing is stored yet"
