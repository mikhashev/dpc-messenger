"""A provider that is waiting out a backoff says so, and can be stopped.

The retry ladder keeps its whole wall-clock budget for every failure the far end
can recover from — a connection that never opened included. That patience is the
point: on 2026-09-02 `api.deepseek.com` was unreachable from this machine for
hours and came back on its own, and a loop that gives up in thirteen seconds
turns a recoverable outage into a failed task.

What bounds the wait instead is whoever is watching it. Each attempt is
announced before it sleeps, under a `retry_id`, so the interface can show
«retry 3, waiting 12s» rather than nothing at all; `cancel_retry(retry_id)`
ends the sleep and any call already in flight, and does it with an ordinary
exception so the request that was abandoned still gets a reply.
"""

import asyncio
import time

import httpx
import pytest

from dpc_client_core.providers import base
from dpc_client_core.providers.base import (
    AIProvider,
    ProviderRetryCancelled,
    cancel_retry,
    never_connected,
)
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider


def _provider(budget=20):
    p = DeepSeekProvider.__new__(DeepSeekProvider)
    p.alias = "deepseek_flash"
    p.max_retry_seconds = budget
    return p


def _wrapped_connect_failure():
    """What the SDK hands us: its own error, caused by httpx's."""
    try:
        try:
            raise httpx.ConnectTimeout("")
        except Exception as inner:
            raise RuntimeError("Request timed out.") from inner
    except Exception as e:
        return e


@pytest.fixture
def observed():
    """Collect what the interface would have been told."""
    events = []
    base.set_retry_observer(lambda ev, payload: events.append((ev, payload)))
    yield events
    base.set_retry_observer(None)


# --- one ladder ---------------------------------------------------------------

def test_every_provider_that_retries_shares_one_ladder():
    """A provider says which name the log carries and what to do with an error
    on its way out; the loop itself has one definition."""
    from dpc_client_core.providers.zai_provider import ZaiProvider
    from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider

    for provider in (DeepSeekProvider, ZaiProvider, LlamaServerProvider):
        assert provider._retry_with_backoff is AIProvider._retry_with_backoff, (
            f"{provider.__name__} carries its own copy of the retry ladder"
        )

    labels = {p.RETRY_LABEL for p in (DeepSeekProvider, ZaiProvider, LlamaServerProvider)}
    assert len(labels) == 3, f"two providers would log under one name: {labels}"


# --- the patience -------------------------------------------------------------

def test_a_connect_failure_keeps_the_whole_budget():
    """The behaviour this file exists to pin.

    An earlier version stopped after two consecutive connect failures. It gave
    up on a live outage in 13s, and the route came back within the hour.
    """
    p = _provider(budget=20)
    calls = []

    async def always_unreachable():
        calls.append(len(calls))
        raise _wrapped_connect_failure()

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(p._retry_with_backoff(always_unreachable, _wrapped_connect_failure()))
    elapsed = time.monotonic() - started

    assert elapsed >= p.max_retry_seconds - 1, (
        f"gave up after {elapsed:.0f}s of a {p.max_retry_seconds}s budget"
    )
    assert len(calls) >= 2, f"only {len(calls)} attempt(s) inside the budget"
    assert "never reached the host" not in str(exc.value), (
        "the early give-up is back"
    )


# --- what the interface is told ------------------------------------------------

def test_each_attempt_is_announced_before_it_waits(observed):
    p = _provider(budget=10)

    async def always_busy():
        raise RuntimeError("429 rate limit")

    with pytest.raises(RuntimeError):
        asyncio.run(p._retry_with_backoff(always_busy, RuntimeError("429 rate limit")))

    retries = [pl for ev, pl in observed if ev == "provider_retry"]
    assert retries, "the interface was told nothing while the provider waited"
    assert [r["attempt"] for r in retries] == list(range(1, len(retries) + 1)), (
        f"attempts are not a run of 1..n: {[r['attempt'] for r in retries]}"
    )
    first = retries[0]
    assert first["alias"] == "deepseek_flash"
    assert first["provider"] == "DeepSeek"
    assert first["budget_seconds"] == 10
    assert first["waiting_seconds"] == 3
    assert first["unreachable"] is False, "a 429 is not an unreachable host"


def test_an_unreachable_attempt_is_marked_as_such(observed):
    p = _provider(budget=5)

    async def always_unreachable():
        raise _wrapped_connect_failure()

    with pytest.raises(RuntimeError):
        asyncio.run(p._retry_with_backoff(always_unreachable, _wrapped_connect_failure()))

    retries = [pl for ev, pl in observed if ev == "provider_retry"]
    assert retries and all(r["unreachable"] is True for r in retries), (
        "a connect failure was not distinguished from a busy service"
    )


def test_the_notice_is_always_closed(observed):
    """An interface showing «retry 3» needs something that clears it, on both
    endings."""
    p = _provider(budget=5)

    async def always_busy():
        raise RuntimeError("503 service unavailable")

    with pytest.raises(RuntimeError):
        asyncio.run(p._retry_with_backoff(always_busy, RuntimeError("503 service unavailable")))
    assert [pl["outcome"] for ev, pl in observed if ev == "provider_retry_finished"] == ["failed"]

    observed.clear()
    attempts = []

    async def busy_then_fine():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("503 service unavailable")
        return "answer"

    result = asyncio.run(
        _provider(budget=20)._retry_with_backoff(busy_then_fine, RuntimeError("503 service unavailable"))
    )
    assert result == "answer"
    assert [pl["outcome"] for ev, pl in observed if ev == "provider_retry_finished"] == ["recovered"]


def test_a_broken_observer_cannot_break_the_retry(observed):
    """The notice is a courtesy; losing it must not lose the recovery."""
    base.set_retry_observer(lambda ev, payload: (_ for _ in ()).throw(ValueError("boom")))
    attempts = []

    async def busy_then_fine():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("429 rate limit")
        return "answer"

    result = asyncio.run(
        _provider(budget=20)._retry_with_backoff(busy_then_fine, RuntimeError("429 rate limit"))
    )
    assert result == "answer"


# --- stopping it ---------------------------------------------------------------

def test_cancelling_the_task_interrupts_the_wait():
    """The bound on the wait, now that the budget is not one.

    Without this the only way out of a ten-minute backoff would be to wait it
    out — which is what made the short give-up look reasonable.
    """
    p = _provider(budget=600)

    async def always_busy():
        raise RuntimeError("429 rate limit")

    async def run_and_cancel():
        task = asyncio.create_task(
            p._retry_with_backoff(always_busy, RuntimeError("429 rate limit"))
        )
        await asyncio.sleep(0.2)          # it is inside the first 3s sleep
        task.cancel()
        started = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    stopped_in = asyncio.run(run_and_cancel())
    assert stopped_in < 1.0, f"cancel took {stopped_in:.1f}s to take effect"


def test_the_notice_carries_the_handle_that_stops_it(observed):
    """A cancel button needs something to send. The id in the notice is it, and
    it is the same id for every attempt of one wait."""
    p = _provider(budget=8)

    async def always_busy():
        raise RuntimeError("429 rate limit")

    with pytest.raises(RuntimeError):
        asyncio.run(p._retry_with_backoff(always_busy, RuntimeError("429 rate limit")))

    retries = [pl for ev, pl in observed if ev == "provider_retry"]
    ids = {r["retry_id"] for r in retries}
    assert len(ids) == 1, f"one wait announced {len(ids)} different ids: {ids}"
    assert next(iter(ids)).startswith("deepseek_flash:")
    closing = [pl for ev, pl in observed if ev == "provider_retry_finished"]
    assert closing[0]["retry_id"] == next(iter(ids)), (
        "the closing notice names a different wait than the one it closes"
    )


def test_the_interface_can_stop_a_wait_by_its_id(observed):
    """What the button does. Ten minutes is the right ceiling for an outage
    nobody is watching and far too long for someone who is.

    The budget here is 20s rather than the real 600 so that a cancel which does
    not work ends the ladder — as a RuntimeError this test refuses — instead of
    leaving it running for ten minutes. A neutralised cancel used to hang here.
    """
    p = _provider(budget=20)

    async def always_busy():
        raise RuntimeError("429 rate limit")

    async def run_and_cancel():
        task = asyncio.create_task(
            p._retry_with_backoff(always_busy, RuntimeError("429 rate limit"))
        )
        await asyncio.sleep(0.2)          # inside the first 3s sleep
        retry_id = observed[0][1]["retry_id"]
        assert cancel_retry(retry_id) is True
        started = time.monotonic()
        with pytest.raises(ProviderRetryCancelled):
            await task
        return time.monotonic() - started, retry_id

    stopped_in, retry_id = asyncio.run(run_and_cancel())
    assert stopped_in < 1.0, f"cancel took {stopped_in:.1f}s to take effect"
    assert [pl["outcome"] for ev, pl in observed if ev == "provider_retry_finished"] == ["cancelled"]
    assert cancel_retry(retry_id) is False, (
        "the finished wait is still in the registry — a later click would "
        "reach whatever inherited the id"
    )


def test_the_cancel_is_an_ordinary_exception():
    """Not `CancelledError`, deliberately.

    Every `except Exception` between the provider and the websocket handler
    would skip a `BaseException`, and the one at the end is what answers the
    request. Cancelling by task cancellation would stop the wait and leave the
    caller with no reply at all — a spinner that never stops.
    """
    assert issubclass(ProviderRetryCancelled, Exception)
    assert not issubclass(ProviderRetryCancelled, asyncio.CancelledError)


def test_a_hung_call_can_be_cancelled_too(observed):
    """The sleep is not the only place the budget is spent: after it, the call
    itself is given everything that is left."""
    p = _provider(budget=600)

    async def never_answers():
        await asyncio.sleep(600)

    async def run_and_cancel():
        task = asyncio.create_task(
            p._retry_with_backoff(never_answers, RuntimeError("429 rate limit"))
        )
        await asyncio.sleep(3.4)          # past the first 3s sleep, inside the call
        assert any(ev == "provider_retry" for ev, _ in observed)
        assert cancel_retry(observed[0][1]["retry_id"]) is True
        started = time.monotonic()
        with pytest.raises(ProviderRetryCancelled):
            await task
        return time.monotonic() - started

    stopped_in = asyncio.run(run_and_cancel())
    assert stopped_in < 1.0, f"cancel took {stopped_in:.1f}s to reach the call"

    # The assertion that makes this test about the call. A flag the call
    # ignores is not caught here — the loop simply abandons the attempt and the
    # NEXT sleep sees it, one announcement later. Counting the announcements is
    # what tells the two apart, and without this the test passed either way.
    assert [pl["attempt"] for ev, pl in observed if ev == "provider_retry"] == [1], (
        "the wait was restarted, so the flag reached the sleep and not the call"
    )
    closing = [pl for ev, pl in observed if ev == "provider_retry_finished"]
    assert closing and closing[0]["attempts"] == 1


def test_an_unknown_id_is_a_no_and_not_a_crash():
    """The ordinary race: the click lands as the call recovers."""
    assert cancel_retry("deepseek_flash:99999") is False
    assert cancel_retry("") is False


def test_the_registry_does_not_leak_a_finished_wait():
    p = _provider(budget=5)

    async def always_busy():
        raise RuntimeError("503 service unavailable")

    before = len(base._retry_waiters)
    with pytest.raises(RuntimeError):
        asyncio.run(p._retry_with_backoff(always_busy, RuntimeError("503 service unavailable")))
    assert len(base._retry_waiters) == before


def test_an_error_with_no_message_still_says_something(observed):
    """`ConnectionError()` is retryable by type, not by text, so `str(e)` can be
    empty and the notice would read «waiting 3s: ». Twice I read this fallback
    as dead code."""
    p = _provider(budget=5)

    async def always_refused():
        raise ConnectionError()

    with pytest.raises(RuntimeError):
        asyncio.run(p._retry_with_backoff(always_refused, ConnectionError()))

    retries = [pl for ev, pl in observed if ev == "provider_retry"]
    assert retries, "a message-less error never reached the loop"
    assert retries[0]["error"] == "ConnectionError"


# --- the notice actually leaves the process ------------------------------------

def test_the_service_carries_the_notice_to_the_websocket():
    """The gap this project keeps falling into: a value computed correctly and
    delivered nowhere. The loop announces; nothing on screen changes unless
    CoreService forwards it.
    """
    from dpc_client_core.service import CoreService

    sent = []

    class _Api:
        async def broadcast_event(self, event, payload):
            sent.append((event, payload))

    svc = CoreService.__new__(CoreService)
    svc.local_api = _Api()

    async def announce_from_inside_a_loop():
        svc._announce_provider_retry("provider_retry", {"attempt": 1})
        await asyncio.sleep(0)      # let the task it created run
        await asyncio.sleep(0)

    asyncio.run(announce_from_inside_a_loop())
    assert sent == [("provider_retry", {"attempt": 1})], (
        f"the interface was never told: {sent}"
    )


def test_the_notice_is_dropped_rather_than_raised_off_the_loop():
    """A provider built and exercised outside asyncio must not turn a
    recoverable error into a crash inside the courtesy notice."""
    from dpc_client_core.service import CoreService

    svc = CoreService.__new__(CoreService)
    svc.local_api = None            # would raise if it were reached
    svc._announce_provider_retry("provider_retry", {"attempt": 1})


def test_the_observer_is_registered_by_the_service():
    """The wiring itself, which no other test would notice going missing."""
    import inspect
    from dpc_client_core import service as service_module

    src = inspect.getsource(service_module.CoreService.__init__)
    assert "set_retry_observer(self._announce_provider_retry)" in src, (
        "nothing registers the observer, so every notice is dropped in base.py"
    )


def test_the_backoff_waits_through_asyncio_sleep():
    """The seam every fake clock in this repo patches.

    `wait_for(flag.wait(), timeout=n)` looks equivalent and is not: it measures
    with the event loop's own timer, which reads `time.monotonic`. A test that
    freezes that — tests/test_a_network_provider_bounds_how_long_one_call_can_wait.py
    does, globally — leaves the timer unable to fire, and the whole suite stalls
    on a wait that can never end. It did, for 25 minutes, before this test.

    Written as an observation rather than a grep so it fails in seconds if the
    wait moves off `asyncio.sleep` again, instead of hanging the way the real
    regression does.
    """
    slept = []

    async def _record(seconds):
        slept.append(seconds)

    async def wait_a_little():
        flag = asyncio.Event()
        real_sleep = asyncio.sleep
        asyncio.sleep = _record
        try:
            await base.sleep_unless_cancelled(0.05, flag)
        finally:
            asyncio.sleep = real_sleep

    asyncio.run(wait_a_little())
    assert slept == [0.05], (
        f"the backoff did not go through asyncio.sleep (recorded {slept}); "
        "a fake clock cannot reach it, and the suite will hang"
    )


# --- the predicate that marks the notice ----------------------------------------

def test_a_connect_failure_is_told_apart_from_a_busy_service():
    assert never_connected(httpx.ConnectTimeout("")) is True
    assert never_connected(httpx.ConnectError("")) is True
    assert never_connected(ConnectionRefusedError()) is True

    assert never_connected(httpx.ReadTimeout("")) is False
    assert never_connected(RuntimeError("429 rate limit")) is False


def test_the_predicate_reads_through_the_sdk_wrapper():
    """The failure never arrives bare — the wrapper's own message says only
    'Request timed out.'"""
    assert never_connected(_wrapped_connect_failure()) is True
