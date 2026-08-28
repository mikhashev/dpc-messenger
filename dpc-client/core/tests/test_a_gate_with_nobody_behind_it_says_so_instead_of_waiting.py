"""A tier-1 command with no surface to ask on is refused, not parked for a minute.

The verdict does not change: a request nobody answers times out and the command
does not run either way. What changes is the sixty seconds of silence and the
word "expired", which reads like a person decided something. Measured on
Johnny's review run: five requests, five minutes of a thirty-four-minute task,
five refusals in the end.

Two surfaces can answer, not one. Telegram carries the card without any
interface client attached, so the refusal is for the case where neither can.
"""
import asyncio
import threading
import time
import types

import pytest

from dpc_client_core.dpc_agent.tools import shell


class _LocalApi:
    def __init__(self, has_clients):
        self.has_clients = has_clients


class _Service:
    """Records what the gate tried to announce, and to whom."""

    def __init__(self, local_api):
        self.local_api = local_api
        self.announced = []

    async def announce_shell_approval_request(self, **kwargs):
        self.announced.append(kwargs)

    async def announce_shell_approval_closed(self, **kwargs):
        pass


def _ctx(tmp_path, *, has_clients=None, telegram_chat_id="", service=True):
    local_api = None if has_clients is None else _LocalApi(has_clients)
    dpc_service = _Service(local_api) if service else None
    return types.SimpleNamespace(
        agent_root=tmp_path,
        dpc_service=dpc_service,
        reply_telegram_chat_id=telegram_chat_id,
        _event_loop=None,
        _agent=None,
    )


@pytest.fixture(autouse=True)
def _no_leftover_requests():
    shell._pending_approvals.clear()
    yield
    shell._pending_approvals.clear()


def _refuse(ctx, command="rm -rf /tmp/x"):
    return shell._request_approval(ctx, command, reason="tier1", cwd="", timeout=10)


# --- refused, and it says why ---


def test_no_interface_and_no_telegram_is_refused_at_once(tmp_path):
    result = _refuse(_ctx(tmp_path, has_clients=False))
    assert "nobody could be asked" in result
    assert "Not run" in result


def test_the_refusal_asks_nobody_and_queues_nothing(tmp_path):
    """Nothing sits waiting, so nothing can be drained by anything either.

    The service loop is real here: without one the announce coroutine is created
    and closed unawaited, and the test would pass whether the gate refused or not.
    """
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        ctx = _ctx(tmp_path, has_clients=False)
        ctx._event_loop = loop
        _refuse(ctx)
        time.sleep(0.2)
        assert ctx.dpc_service.announced == []
        assert shell._pending_approvals == {}
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()


def test_a_service_without_a_local_api_is_the_same_answer(tmp_path):
    result = _refuse(_ctx(tmp_path, has_clients=None))
    assert "nobody could be asked" in result


def test_no_service_at_all_is_the_same_answer(tmp_path):
    result = _refuse(_ctx(tmp_path, service=False))
    assert "nobody could be asked" in result


def test_the_word_expired_is_not_used_for_a_question_never_asked(tmp_path):
    """It used to say the same thing a human refusal says."""
    result = _refuse(_ctx(tmp_path, has_clients=False))
    assert "expired" not in result.lower()
    assert "timed out" not in result.lower()


# --- and it asks whenever anything can answer ---


def test_a_connected_interface_still_gets_the_question(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 0.05)
    ctx = _ctx(tmp_path, has_clients=True)
    result = _refuse(ctx)
    assert "nobody could be asked" not in result
    assert "approval timed out" in result.lower()


def test_a_telegram_run_is_asked_even_with_no_interface_client(tmp_path, monkeypatch):
    """Telegram answers without a desktop attached; refusing here would be
    stricter than production is today, which this change must not be."""
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 0.05)
    ctx = _ctx(tmp_path, has_clients=False, telegram_chat_id="123456789")
    result = _refuse(ctx)
    assert "nobody could be asked" not in result
    assert "approval timed out" in result.lower()
