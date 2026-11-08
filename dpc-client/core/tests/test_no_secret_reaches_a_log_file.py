"""No secret reaches a log file: by field name in a UI payload, by shape everywhere.

local_api logs every UI command's payload at DEBUG, and `_redact_secrets` used to hide
only `api_key`, a key pasted into `api_key_env` and sk- values, so the Telegram bot token
that `link_agent_telegram` re-sends on every Save of the Telegram dialog was written to
dpc-client.log in clear - a file agents read through run_shell. ui.log mirrors every
console line of the UI as free text. Card
THE-LOCAL-API-LOG-CARRIES-PROVIDER-KEYS-AND-BOT-TOKENS-IN-CLEAR (Mike's order, 2026-11-08).

Three layers, each tested here through the path that writes: the payload log and the
error-detail log in local_api, the ui_log relay, and a filter on every handler
setup_logging builds, the backstop for a path nobody named. Every value is fake.
"""

from __future__ import annotations

import json
import logging
import sys
import timeit

import pytest

import run_service
from dpc_client_core.local_api import (
    LocalApiServer,
    _log_error_under_ok,
    _redact_secrets,
    _redact_secrets_in_text,
    _sanitize_payload_for_logging,
)
from dpc_client_core.message_handlers.telegram_handler import TelegramIncomingHandler
from dpc_client_core.settings import Settings

# 35 characters after the colon, as BotFather issues them.
TG = "123456789:AA" + "FAKE" * 8 + "x"
SK = "sk-FAKEfakeFAKEfakeFAKEfake0123"
SK_ANT = "sk-ant-api03-FAKEfakeFAKEfakeFAKEfake0123"
AIZA = "AIza" + "FAKEfake" * 4 + "012"
HF = "hf_" + "FAKEfake" * 4
GHP = "ghp_" + "FAKEfake" * 4 + "0123"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.FAKEfakeFAKEfake"
PLAIN = "plainFAKEsecretWithNoShape"
SHAPES = (TG, SK, SK_ANT, AIZA, HF, GHP, JWT)


class _Capture(logging.Handler):
    """Records the formatted lines of one named logger, whatever its propagate flag."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture
def capture():
    attached = []

    def _attach(name):
        lg = logging.getLogger(name)
        h = _Capture()
        attached.append((lg, h, lg.level))
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
        return h

    yield _attach
    for lg, h, level in attached:
        lg.removeHandler(h)
        lg.setLevel(level)


class _FakeWS:
    def __init__(self, frames):
        self._frames = list(frames)
        self.remote_address = ("127.0.0.1", 0)
        self.sent = []

    async def recv(self):
        return self._frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, *args, **kwargs):
        pass


async def _drive(core, *commands):
    server = LocalApiServer(core, port=0)
    server._auth_token = "token"
    frames = [json.dumps({"command": "auth", "token": "token"})]
    frames += [json.dumps({"id": str(i), "command": c, "payload": p})
               for i, (c, p) in enumerate(commands)]
    ws = _FakeWS(frames)
    await server._handler(ws)
    return ws


# --- the payload log ----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_bot_token_of_link_agent_telegram_is_not_in_the_executing_line(capture):
    seen = {}

    class FakeCore:
        async def link_agent_telegram(self, agent_id, bot_token, chat_ids, **kw):
            seen["token"] = bot_token
            return {"status": "success"}

    log = capture("dpc_client_core.local_api")
    payload = {"agent_id": "agent_001", "bot_token": TG, "chat_ids": ["42"],
               "owner_user_id": "42", "max_events_per_minute": 20}
    await _drive(FakeCore(), ("link_agent_telegram", payload))

    executing = [l for l in log.lines if "Executing command 'link_agent_telegram'" in l]
    assert executing, "the payload line must still be written"
    assert TG not in log.text and TG.split(":")[1] not in log.text
    assert "agent_001" in executing[0] and "'owner_user_id': '42'" in executing[0]
    assert seen["token"] == TG, "redaction is for the log, the handler gets the token"


@pytest.mark.asyncio
async def test_a_providers_config_logs_no_key_in_any_field(capture):
    class FakeCore:
        async def save_providers_config(self, config_dict, alias_renames=None):
            return {"status": "success"}

    log = capture("dpc_client_core.local_api")
    config = {"providers": [
        {"alias": "a", "type": "anthropic", "api_key": PLAIN, "api_key_env": "ANTHROPIC_API_KEY"},
        {"alias": "d", "type": "deepseek", "api_key_env": SK},  # a key pasted as the name
        {"alias": "o", "type": "openai", "notes": f"mine is {SK_ANT}"},  # innocent name
        {"alias": "g", "type": "gemini", "base_url": f"https://x.example/?key={AIZA}"},
    ], "default_provider": "a"}
    await _drive(FakeCore(), ("save_providers_config", {"config_dict": config}))

    for secret in (PLAIN, SK, SK_ANT, AIZA):
        assert secret not in log.text
    assert "ANTHROPIC_API_KEY" in log.text, "a variable name is what one debugs by"
    assert "'notes': 'mine is sk-<redacted>'" in log.text


def test_every_secret_word_as_a_name_or_an_ending_is_redacted():
    payload = {"key": PLAIN, "bot_token": PLAIN, "client_secret": PLAIN,
               "password": PLAIN, "credential": PLAIN, "apiKey": PLAIN, "authToken": PLAIN,
               "nested": [{"turn": {"credential": PLAIN}}]}
    logged = _redact_secrets(payload)
    assert PLAIN not in repr(logged)
    assert logged["nested"][0]["turn"]["credential"] == "<redacted>"


def test_what_is_not_a_secret_stays_readable():
    payload = {"api_key_env": "DEEPSEEK_API_KEY", "set_key": "general",
               "max_tokens": 4096, "history_tokens": "1200", "model": "sk-looking-model-name",
               "text": "risk-free sandisk-dc-sn861-7-68tb-review at 10:55:44"}
    assert _sanitize_payload_for_logging(payload) == payload


def test_a_payload_string_gets_the_shape_pass():
    logged = _redact_secrets({"text": f"token {TG} and {HF} and {GHP} and {JWT}"})
    for secret in (TG, HF, GHP, JWT):
        assert secret not in logged["text"]
    assert logged["text"].startswith("token 123456789:<redacted> and hf_<redacted> and ghp_<redacted>")


# --- the error detail -----------------------------------------------------------

def test_an_error_detail_quoting_a_token_or_a_key_pair_is_masked(capture):
    log = capture("dpc_client_core.local_api")
    _log_error_under_ok("link_agent_telegram", {
        "status": "error",
        "message": f"bot {TG} refused; config {{'bot_token': '{PLAIN}'}}"})
    assert log.lines, "the line itself must be written"
    assert TG not in log.text and PLAIN not in log.text
    assert "link_agent_telegram" in log.text


# --- ui_log -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_ui_log_line_carrying_a_config_or_a_bare_token_is_redacted(capture):
    log = capture("dpc_ui")
    console_line = 'Providers loaded: {"providers":[{"alias":"x","api_key": "' + SK + '"},' \
                   '{"alias":"y","api_key":"' + PLAIN + '"}]} and ' + TG
    escaped = json.dumps(json.dumps({"bot_token": PLAIN}))  # JSON inside a JSON string
    await _drive(object(),
                 ("ui_log", {"level": "info", "context": "Providers", "message": console_line}),
                 ("ui_log", {"level": "warn", "context": "Telegram", "message": escaped}),
                 ("ui_log", {"level": "info", "context": "ui", "message": "plain text, set_key 'general'"}))

    assert len(log.lines) == 3
    for secret in (SK, PLAIN, TG):
        assert secret not in log.text
    assert '"api_key": "<redacted>"' in log.lines[0]
    assert '"alias":"x"' in log.lines[0]
    assert log.lines[2] == "[ui] plain text, set_key 'general'"


# --- other call sites ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_telegram_config_change_logs_field_names_only(capture):
    handler = TelegramIncomingHandler.__new__(TelegramIncomingHandler)
    handler.logger = logging.getLogger("tests.telegram_config_change")
    log = capture("tests.telegram_config_change")
    await handler._handle_config_change({"bot_token": PLAIN, "enabled": True})
    assert PLAIN not in log.text
    assert "bot_token" in log.text


# --- the backstop ---------------------------------------------------------------------

def _record(msg, args=(), exc_info=None, name="tests.backstop"):
    return logging.LogRecord(name, logging.INFO, __file__, 1, msg, args, exc_info)


def test_the_backstop_formats_the_args_then_redacts():
    record = _record("HTTP Request: %s %s", ("POST", f"https://api.telegram.org/bot{TG}/getUpdates"))
    assert run_service._SecretRedactingFilter().filter(record) is True
    line = logging.Formatter("%(message)s").format(record)
    assert TG not in line
    assert line == "HTTP Request: POST https://api.telegram.org/bot123456789:<redacted>/getUpdates"
    assert record.args is None


def test_the_backstop_redacts_an_exception_text_and_keeps_the_traceback():
    try:
        raise RuntimeError(f"The token `{TG}` was rejected by the server.")
    except RuntimeError:
        record = _record("start failed", exc_info=sys.exc_info())
    run_service._SecretRedactingFilter().filter(record)
    line = logging.Formatter("%(message)s").format(record)
    assert TG not in line
    assert "Traceback (most recent call last)" in line and "RuntimeError" in line


def test_the_backstop_takes_a_non_string_message_and_a_broken_one():
    f = run_service._SecretRedactingFilter()
    as_dict = _record({"holder": TG})
    assert f.filter(as_dict) and TG not in logging.Formatter("%(message)s").format(as_dict)
    number = _record(12345)
    assert f.filter(number) and number.getMessage() == "12345"
    broken = _record("%d items", ("not a number",))
    assert f.filter(broken) is True  # left for the handler to report, not raised here


@pytest.fixture
def clean_logging_state():
    root = logging.getLogger()
    ui = logging.getLogger("dpc_ui")
    saved = (root.level, list(root.handlers), ui.level, list(ui.handlers), ui.propagate)
    yield
    for lg, before in ((root, saved[1]), (ui, saved[3])):
        for h in list(lg.handlers):
            if h not in before:
                lg.removeHandler(h)
                h.close()
    root.setLevel(saved[0])
    ui.setLevel(saved[2])
    ui.propagate = saved[4]


def test_both_log_files_setup_logging_builds_redact_by_shape(tmp_path, clean_logging_state):
    log_file = tmp_path / "dpc-client.log"
    (tmp_path / "config.ini").write_text(
        "[logging]\nlevel = DEBUG\nconsole = false\n"
        f"file = {log_file.as_posix()}\nmax_bytes = 10485760\nbackup_count = 1\n",
        encoding="utf-8")
    run_service.setup_logging(Settings(tmp_path))

    logging.getLogger("tests.backstop.library").info("calling %s", f"/bot{TG}/getMe")
    logging.getLogger("dpc_ui").info("[x] %s", f"pasted {SK}")
    for h in logging.getLogger().handlers + logging.getLogger("dpc_ui").handlers:
        h.flush()

    main = log_file.read_text(encoding="utf-8")
    ui = (tmp_path / "ui.log").read_text(encoding="utf-8")
    assert "calling /bot123456789:<redacted>/getMe" in main and TG not in main
    assert "pasted sk-<redacted>" in ui and SK not in ui


def test_the_backstop_costs_microseconds_per_record():
    f = run_service._SecretRedactingFilter()
    text = "Executing command 'get_status' with payload: {'conversation_id': 'group-b88b65076b85'}"

    def one():
        f.filter(_record("%s", (text,)))

    per_record = min(timeit.repeat(one, number=10_000, repeat=3)) / 10_000
    print(f"backstop: {per_record * 1e6:.1f} us per record")
    assert per_record < 1e-3


def test_every_shape_is_recognised_in_free_text():
    text = " | ".join(SHAPES)
    redacted = _redact_secrets_in_text(text)
    for secret in SHAPES:
        assert secret not in redacted
