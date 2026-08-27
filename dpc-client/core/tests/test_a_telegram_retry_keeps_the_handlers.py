"""A bot that survives a flaky startup must still be able to hear.

Measured 2026-08-27: one `NetworkError` at 04:02:17 while starting, and the bot
spent the whole day fetching every incoming message and discarding it, because
the retry built a fresh Application and the handlers had been attached to the
one that failed. Sending still worked — `bot.send_message` needs no handler —
so from the outside it looked like nobody was writing to the bot.

The correlation over one log: 08-25 no retries and 8 inbound handled, 08-26 no
retries and 13, 08-27 one retry and none.
"""
import asyncio
import types

import pytest

from dpc_client_core.managers.telegram_manager import TelegramBotManager
from telegram.error import NetworkError


class _FakeUpdater:
    def __init__(self):
        self.polling_started = False

    async def start_polling(self, **kwargs):
        self.polling_started = True


class _FakeApplication:
    """Records what was attached to it, and whether it ever polled."""

    def __init__(self, fail_initialize: bool):
        self.handlers = []
        self.error_handlers = []
        self.updater = _FakeUpdater()
        self.bot = types.SimpleNamespace()
        self._fail_initialize = fail_initialize

    def add_handler(self, handler):
        self.handlers.append(handler)

    def add_error_handler(self, handler):
        self.error_handlers.append(handler)

    async def initialize(self):
        if self._fail_initialize:
            raise NetworkError("Timed out")

    async def start(self):
        pass


class _FakeBuilder:
    def __init__(self, apps):
        self._apps = apps

    def token(self, _token):
        return self

    def build(self):
        return self._apps.pop(0)


def _bridge():
    async def _noop(update, context):
        return None

    return types.SimpleNamespace(
        handle_text_message=_noop, handle_voice_message=_noop,
        handle_photo_message=_noop, handle_document_message=_noop,
        handle_video_message=_noop,
    )


def _service(bridge):
    """Enough of CoreService for the startup path: a bridge and a UI channel."""
    async def _broadcast(name, payload):
        return None

    return types.SimpleNamespace(
        telegram_bridge=bridge,
        local_api=types.SimpleNamespace(broadcast_event=_broadcast),
    )


def _manager(apps, monkeypatch):
    import telegram.ext as telegram_ext

    bridge = _bridge()
    service = _service(bridge)
    mgr = TelegramBotManager(service, {
        "bot_token": "123:fake", "allowed_chat_ids": ["1"],
        "use_webhook": False, "fetch_history_on_startup": False,
    })

    fake_application = types.SimpleNamespace(builder=lambda: _FakeBuilder(apps))
    monkeypatch.setattr(telegram_ext, "Application", fake_application)

    async def _no_sender_loop():
        return None

    monkeypatch.setattr(mgr, "_message_sender_loop", _no_sender_loop)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda d, *a, **k: real_sleep(0))
    return mgr, bridge


def _run(mgr):
    async def _go():
        await mgr.start()
        if mgr._sender_task is not None:
            mgr._sender_task.cancel()
            await asyncio.gather(mgr._sender_task, return_exceptions=True)

    asyncio.run(_go())


# --- the defect ---


def test_the_application_that_polls_after_a_retry_has_its_handlers(monkeypatch):
    failed = _FakeApplication(fail_initialize=True)
    survivor = _FakeApplication(fail_initialize=False)
    mgr, _ = _manager([failed, survivor], monkeypatch)

    _run(mgr)

    assert survivor.updater.polling_started, "the second application is the one that polls"
    assert len(survivor.handlers) == 5, (
        "the application that polls carries no handlers: it would fetch every "
        "update and drop it, which reads as silence"
    )


def test_the_handlers_point_at_the_bridge_and_not_at_nothing(monkeypatch):
    """Five objects is not the claim; five live callbacks is."""
    failed = _FakeApplication(fail_initialize=True)
    survivor = _FakeApplication(fail_initialize=False)
    mgr, bridge = _manager([failed, survivor], monkeypatch)

    _run(mgr)

    callbacks = {h.callback for h in survivor.handlers}
    assert bridge.handle_voice_message in callbacks
    assert bridge.handle_text_message in callbacks


def test_a_clean_start_is_unchanged(monkeypatch):
    """The path that worked on 08-25 and 08-26 must keep working."""
    only = _FakeApplication(fail_initialize=False)
    mgr, _ = _manager([only], monkeypatch)

    _run(mgr)

    assert only.updater.polling_started
    assert len(only.handlers) == 5


def test_two_retries_still_leave_one_fully_wired_application(monkeypatch):
    """The rebuild happens once per failure, and so must the wiring."""
    apps = [_FakeApplication(True), _FakeApplication(True), _FakeApplication(False)]
    mgr, _ = _manager(list(apps), monkeypatch)

    _run(mgr)

    assert apps[-1].updater.polling_started
    assert len(apps[-1].handlers) == 5


def test_the_bot_is_not_started_at_all_without_a_bridge(monkeypatch):
    """Refusing is the existing behaviour and the safer half — pin it."""
    import telegram.ext as telegram_ext

    app = _FakeApplication(fail_initialize=False)
    mgr = TelegramBotManager(_service(None), {
        "bot_token": "123:fake", "allowed_chat_ids": ["1"],
        "use_webhook": False, "fetch_history_on_startup": False,
    })
    monkeypatch.setattr(telegram_ext, "Application",
                        types.SimpleNamespace(builder=lambda: _FakeBuilder([app])))

    asyncio.run(mgr.start())

    assert app.handlers == []
    assert not app.updater.polling_started
    assert mgr._running is False
