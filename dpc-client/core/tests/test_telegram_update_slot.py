"""A button press must be answerable while the run that raised it is still going.

This is the mechanism of the 2026-08-15 incident, and the only test that touches
it: a real python-telegram-bot Application, the handlers registered exactly as
production registers them, a message handler that does not return, and a
callback_query that has to be processed anyway.

The handler callbacks are swapped for probes; the `block` flags are production's
own, so removing `block=False` from the registration turns this red.
"""

import asyncio
import datetime

import pytest
from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, MessageHandler

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge


def _text_update(update_id: int, chat_id: int = 77) -> Update:
    user = User(id=chat_id, first_name="Mike", is_bot=False)
    chat = Chat(id=chat_id, type=Chat.PRIVATE)
    message = Message(
        message_id=update_id,
        date=datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc),
        chat=chat,
        from_user=user,
        text="run something that needs approval",
    )
    return Update(update_id=update_id, message=message)


def _callback_update(update_id: int, data: str, chat_id: int = 77) -> Update:
    user = User(id=chat_id, first_name="Mike", is_bot=False)
    chat = Chat(id=chat_id, type=Chat.PRIVATE)
    message = Message(
        message_id=update_id,
        date=datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc),
        chat=chat,
        from_user=user,
        text="Agent wants to run a shell command",
    )
    query = CallbackQuery(
        id=str(update_id), from_user=user, chat_instance="ci", data=data, message=message
    )
    return Update(update_id=update_id, callback_query=query)


@pytest.mark.asyncio
async def test_a_press_is_processed_while_a_run_is_still_in_flight():
    bridge = AgentTelegramBridge(bot_token="123456:TESTTOKEN", allowed_chat_ids=["77"])

    app = ApplicationBuilder().token("123456:TESTTOKEN").build()
    # initialize() would fetch the bot identity over the network. Update
    # dispatch needs the flag and the cached identity, and nothing else.
    app._initialized = True
    app.bot._bot_user = User(id=1, first_name="testbot", is_bot=True, username="testbot")
    bridge._register_handlers(app)

    # Bound methods are rebuilt on each attribute access, so compare by equality.
    text_handler = next(
        h for group in app.handlers.values() for h in group
        if isinstance(h, MessageHandler) and h.callback == bridge._handle_message
    )
    shell_handler = next(
        h for group in app.handlers.values() for h in group
        if isinstance(h, CallbackQueryHandler) and h.callback == bridge._handle_shell_callback
    )

    run_started = asyncio.Event()
    release_run = asyncio.Event()
    press_handled = asyncio.Event()

    async def slow_run(update, context):
        run_started.set()
        await release_run.wait()

    async def quick_press(update, context):
        press_handled.set()

    text_handler.callback = slow_run
    shell_handler.callback = quick_press

    # Bounded on purpose: a handler that holds the update slot would never give
    # this call back, and a hanging test says nothing.
    await asyncio.wait_for(app.process_update(_text_update(1)), timeout=2)
    await asyncio.wait_for(run_started.wait(), timeout=2)

    await app.process_update(_callback_update(2, "shell:abc123:approve"))
    await asyncio.wait_for(press_handled.wait(), timeout=2)

    assert run_started.is_set() and not release_run.is_set(), "the run must still be in flight"

    release_run.set()
    await asyncio.sleep(0)
