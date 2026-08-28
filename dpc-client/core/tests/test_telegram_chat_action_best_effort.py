"""A Telegram activity indicator must never cost the user's message.

`send_chat_action` is decoration — "typing…", "uploading voice". Three of the
four call sites had one outside any try, so an httpx timeout on the indicator
aborted the handler and the message was dropped with nothing in the log. What
is asserted here is the property, not the placement: every handler still runs
its real work when the indicator raises.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge

CHAT_ID = "424242"


def _bridge():
    b = AgentTelegramBridge(bot_token="t", allowed_chat_ids=[CHAT_ID])
    b._message_handler = AsyncMock(return_value="agent reply")
    return b


def _failing_context():
    ctx = SimpleNamespace(bot=SimpleNamespace())
    ctx.bot.send_chat_action = AsyncMock(side_effect=TimeoutError("httpx ConnectTimeout"))
    return ctx


def _update(text="hello"):
    u = MagicMock()
    u.effective_chat.id = CHAT_ID
    u.effective_user.first_name = "Mike"
    u.message.text = text
    u.message.reply_text = AsyncMock()
    return u


@pytest.mark.asyncio
async def test_failing_indicator_does_not_raise():
    await _bridge()._show_chat_action(_failing_context(), CHAT_ID, "typing")


@pytest.mark.asyncio
async def test_text_message_survives_a_failing_indicator():
    bridge, ctx = _bridge(), _failing_context()

    await bridge._handle_message(_update(), ctx)

    ctx.bot.send_chat_action.assert_awaited()  # the indicator really was tried
    bridge._message_handler.assert_awaited()   # and the message still reached the agent


def test_every_call_site_goes_through_the_helper():
    """The guarantee is structural — a new handler cannot reintroduce the bug.

    Asserting only on _handle_message would leave the voice and photo handlers
    free to call the raw API again, which is exactly how this defect survived
    its first fix.
    """
    src = inspect.getsource(AgentTelegramBridge)
    direct = src.count("bot.send_chat_action(")
    assert direct == 1, (
        f"{direct} direct send_chat_action calls; only _show_chat_action may make one"
    )
