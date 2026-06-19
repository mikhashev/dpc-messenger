"""The agent Telegram bridge must refuse to start a second poller on a token
already polled in this process (would cause a Telegram getUpdates Conflict)."""

import pytest

from dpc_client_core.managers import agent_telegram_bridge as atb
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge


@pytest.fixture(autouse=True)
def _clear_tokens():
    atb._ACTIVE_BOT_TOKENS.clear()
    yield
    atb._ACTIVE_BOT_TOKENS.clear()


@pytest.mark.asyncio
async def test_second_bridge_on_same_token_refused():
    token = "123456:TESTTOKEN"
    atb._ACTIVE_BOT_TOKENS.add(token)  # simulate a live bridge already polling this token
    bridge = AgentTelegramBridge(bot_token=token, allowed_chat_ids=["429727247"])
    # Guard returns before any telegram import / network call.
    assert await bridge.start() is False


@pytest.mark.asyncio
async def test_empty_token_refused():
    bridge = AgentTelegramBridge(bot_token="", allowed_chat_ids=["1"])
    assert await bridge.start() is False
