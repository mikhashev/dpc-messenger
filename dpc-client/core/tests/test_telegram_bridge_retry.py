"""A network failure at startup must not cost the channel the whole session.

2026-08-14 it did: the machine came back before its network, the one start attempt
hit `httpx.ConnectError`, and the agent bridge stayed down from 12:56 to 16:46 —
found only because the operator wrote to a bot nobody was listening to. These tests
hold the three properties that fix rests on, because a retry is an asynchronous
state machine and that is the class that regresses without a sound.
"""

import asyncio

import pytest
from telegram.error import NetworkError

from dpc_client_core.managers import agent_telegram_bridge as atb
from dpc_client_core.managers.agent_telegram_bridge import AgentTelegramBridge


@pytest.fixture(autouse=True)
def _clear_tokens():
    atb._ACTIVE_BOT_TOKENS.clear()
    yield
    atb._ACTIVE_BOT_TOKENS.clear()


@pytest.fixture
def no_backoff(monkeypatch):
    """Run the ladder without waiting out its 10s → 20s → … delays."""
    delays = []
    real_sleep = asyncio.sleep

    async def _instant(seconds):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(atb.asyncio, "sleep", _instant)
    return delays


def _bridge(**kw):
    return AgentTelegramBridge(bot_token="123456:TESTTOKEN", allowed_chat_ids=["1"], **kw)


@pytest.mark.asyncio
async def test_network_failure_schedules_a_retry_and_the_bridge_comes_back(no_backoff):
    bridge = _bridge()
    wired = []
    bridge._on_started = lambda: wired.append(1)
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise NetworkError("httpx.ConnectError: All connection attempts failed")
        bridge._enabled = True
        bridge._on_started()
        return True

    bridge._start_once = flaky

    assert await bridge.start() is False, "a start that failed must not report success"
    assert bridge._retry_task is not None, "no retry was scheduled"
    await bridge._retry_task

    assert attempts["n"] == 3
    assert no_backoff == [10, 20], "the ladder must match the sibling manager's"
    assert bridge._enabled is True
    assert wired == [1], "the owner must be wired exactly once, on the recovery"


@pytest.mark.asyncio
async def test_a_bridge_that_refuses_for_a_non_network_reason_is_not_retried(no_backoff):
    """Only unreachability is worth retrying — a misconfigured bridge stays down."""
    bridge = _bridge()

    async def refuses():
        return False

    bridge._start_once = refuses

    assert await bridge.start() is False
    assert bridge._retry_task is None, "a permanent refusal must not spawn a retry loop"


@pytest.mark.asyncio
async def test_stopping_cancels_a_pending_retry(no_backoff):
    """Otherwise the bridge climbs back up minutes after someone shut it down."""
    bridge = _bridge()
    gate = asyncio.Event()

    async def never_succeeds():
        gate.set()
        raise NetworkError("still unreachable")

    bridge._start_once = never_succeeds

    assert await bridge.start() is False
    task = bridge._retry_task
    assert task is not None
    await gate.wait()

    await bridge.stop()
    assert bridge._retry_task is None
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
