"""A getUpdates Conflict is logged with the bot's name and a rolling repeat count.

PTB's own log line carries no bot name; DPC runs several pollers, so the line
was useless. The owner bot and every agent bridge hand the shared callback to
Updater.start_polling. No network is touched here.
"""

import logging
from types import SimpleNamespace

import pytest
from telegram.error import Conflict, NetworkError

from dpc_client_core.managers import _telegram_conflict as tc

TOKEN = "123456:SECRET-TOKEN-VALUE"


def _conflict():
    return Conflict("terminated by other getUpdates request; make sure that only one bot instance is running")


def test_the_line_names_the_bot_and_never_the_token(caplog):
    cb = tc.make_polling_error_callback(lambda: "pttraderbot", "agent bridge for agent_x")
    with caplog.at_level(logging.ERROR, logger=tc.log.name):
        cb(_conflict())
    text = caplog.text
    assert "@pttraderbot (agent bridge for agent_x)" in text
    assert "repeat 1 in the last 10 min" in text
    assert "SECRET" not in text and TOKEN not in text


def test_repeats_are_counted_and_the_threshold_says_second_instance(caplog):
    cb = tc.make_polling_error_callback(lambda: "b", "owner bot", threshold=3)
    with caplog.at_level(logging.ERROR, logger=tc.log.name):
        for _ in range(3):
            cb(_conflict())
    lines = [r.getMessage() for r in caplog.records]
    assert "repeat 1 " in lines[0] and "second instance" not in lines[0]
    assert "repeat 2 " in lines[1] and "second instance" not in lines[1]
    assert "repeat 3 " in lines[2] and "likely a second instance polling this token (another node?)" in lines[2]


def test_old_conflicts_leave_the_window():
    now = [0.0]
    counter = tc.ConflictCounter(window=600, clock=lambda: now[0])
    assert counter.hit() == 1
    now[0] = 300
    assert counter.hit() == 2
    now[0] = 700  # the first one is older than 10 min, the second is not
    assert counter.hit() == 2


def test_each_bot_counts_on_its_own(caplog):
    a = tc.make_polling_error_callback(lambda: "a", "owner bot")
    b = tc.make_polling_error_callback(lambda: "b", "owner bot")
    with caplog.at_level(logging.ERROR, logger=tc.log.name):
        a(_conflict())
        a(_conflict())
        b(_conflict())
    assert "repeat 1 " in caplog.records[-1].getMessage()


def test_username_failure_does_not_break_the_callback(caplog):
    def boom():
        raise RuntimeError("not initialized")
    cb = tc.make_polling_error_callback(boom, "owner bot")
    with caplog.at_level(logging.ERROR, logger=tc.log.name):
        cb(_conflict())
    assert "@? (owner bot)" in caplog.text


def test_other_errors_keep_ptbs_own_traceback_and_get_no_conflict_line(caplog):
    cb = tc.make_polling_error_callback(lambda: "b", "owner bot")
    with caplog.at_level(logging.ERROR):
        cb(NetworkError("getaddrinfo failed"))
    assert "Conflict" not in caplog.text
    rec = caplog.records[-1]
    assert rec.name == "telegram.ext.Updater"
    assert "Exception happened while polling for updates." in rec.getMessage()
    assert rec.exc_info is not None


# --- both pollers register it ------------------------------------------------

class _FakeUpdater:
    def __init__(self):
        self.kwargs = None

    async def start_polling(self, **kwargs):
        self.kwargs = kwargs


class _FakeBot:
    username = "fakebot"

    async def get_me(self):
        return SimpleNamespace(username="fakebot")


class _FakeApp:
    def __init__(self):
        self.updater = _FakeUpdater()
        self.bot = _FakeBot()

    def add_handler(self, *a, **k): pass
    def add_error_handler(self, *a, **k): pass
    async def initialize(self): pass
    async def start(self): pass


class _FakeBuilder:
    def __init__(self, app):
        self._app = app
    def token(self, _t): return self
    def request(self, _r): return self
    def build(self): return self._app


@pytest.mark.asyncio
async def test_an_agent_bridge_passes_the_callback_to_start_polling(monkeypatch, caplog):
    from telegram.ext import Application
    from dpc_client_core.managers import agent_telegram_bridge as atb

    atb._ACTIVE_BOT_TOKENS.clear()
    app = _FakeApp()
    monkeypatch.setattr(Application, "builder", staticmethod(lambda: _FakeBuilder(app)))
    bridge = atb.AgentTelegramBridge(bot_token=TOKEN, allowed_chat_ids=["1"], agent_id="agent_x")
    try:
        assert await bridge._start_once() is True
        cb = app.updater.kwargs["error_callback"]
        with caplog.at_level(logging.ERROR, logger=tc.log.name):
            cb(_conflict())
        assert "@fakebot (agent bridge for agent_x)" in caplog.text
        assert TOKEN not in caplog.text
    finally:
        atb._ACTIVE_BOT_TOKENS.clear()


@pytest.mark.asyncio
async def test_the_owner_bot_passes_the_callback_to_start_polling(monkeypatch, caplog):
    from telegram.ext import Application
    from dpc_client_core.managers.telegram_manager import TelegramBotManager

    app = _FakeApp()
    monkeypatch.setattr(Application, "builder", staticmethod(lambda: _FakeBuilder(app)))

    async def _noop(*a, **k): pass

    service = SimpleNamespace(
        telegram_bridge=SimpleNamespace(),
        local_api=SimpleNamespace(broadcast_event=_noop),
    )
    mgr = TelegramBotManager(service, {
        "bot_token": TOKEN, "allowed_chat_ids": ["1"],
        "fetch_history_on_startup": False, "drop_pending_updates": True,
    })
    monkeypatch.setattr(mgr, "_install_message_handlers", lambda *a, **k: None)
    try:
        await mgr.start()
        cb = app.updater.kwargs["error_callback"]
        with caplog.at_level(logging.ERROR, logger=tc.log.name):
            cb(_conflict())
        assert "@fakebot (owner bot)" in caplog.text
        assert TOKEN not in caplog.text
    finally:
        t = getattr(mgr, "_sender_task", None)
        if t is not None:
            t.cancel()
