"""A route that is gone gives the same answer every time; stop asking it.

Measured 2026-09-02 from Mike's log, with `api.deepseek.com` unreachable from this
machine. Subtracting each sleep from the interval between retry lines gives the
same number every pass:

    retry 2  elapsed 13   13-3   = 10s spent in the call
    retry 3  elapsed 29   16-6   = 10s
    retry 4  elapsed 51   22-12  = 10s
    retry 5  elapsed 85   34-24  = 10s
    retry 6  elapsed 143  58-48  = 10s

Ten seconds is a connect timeout, not a slow answer — and confirmed separately:
DNS resolved in 0.25s while TCP to the resolved address timed out on both 80 and
443, with api.telegram.org, api.anthropic.com and github.com all reachable in
about a second. `_is_retryable` puts a connect failure in the same set as a 429,
so the ladder spent the whole 600s budget re-asking a question already answered.

The budget itself is not the defect and is not changed here: a service that
answers "busy" still gets the full ten minutes.
"""

import asyncio
import time

import httpx
import pytest

from dpc_client_core.providers.base import never_connected
from dpc_client_core.providers.deepseek_provider import DeepSeekProvider


def _provider():
    """A provider reduced to the retry loop under test."""
    p = DeepSeekProvider.__new__(DeepSeekProvider)
    p.alias = "deepseek_flash"
    p.max_retry_seconds = 600
    return p


def _wrapped_connect_failure():
    """What the SDK actually hands us: its own error, caused by httpx's."""
    try:
        try:
            raise httpx.ConnectTimeout("")
        except Exception as inner:
            raise RuntimeError("Request timed out.") from inner
    except Exception as e:
        return e


# --- one ladder ---------------------------------------------------------------

def test_every_provider_that_retries_shares_one_ladder():
    """The fix above went into two copies by hand before this was true.

    A provider says which name the log carries and what to do with an error on
    its way out; the loop itself has one definition.
    """
    from dpc_client_core.providers.base import AIProvider
    from dpc_client_core.providers.zai_provider import ZaiProvider
    from dpc_client_core.providers.llamacpp_server_provider import LlamaServerProvider

    for provider in (DeepSeekProvider, ZaiProvider, LlamaServerProvider):
        assert provider._retry_with_backoff is AIProvider._retry_with_backoff, (
            f"{provider.__name__} carries its own copy of the retry ladder"
        )

    labels = {p.RETRY_LABEL for p in (DeepSeekProvider, ZaiProvider, LlamaServerProvider)}
    assert len(labels) == 3, f"two providers would log under one name: {labels}"


# --- the predicate -----------------------------------------------------------

def test_a_connect_failure_is_told_apart_from_a_busy_service():
    assert never_connected(httpx.ConnectTimeout("")) is True
    assert never_connected(httpx.ConnectError("")) is True
    assert never_connected(ConnectionRefusedError()) is True

    # The control, and the reason this is a predicate rather than a blanket rule:
    # these mean the service answered, and they keep the full budget.
    assert never_connected(httpx.ReadTimeout("")) is False
    assert never_connected(RuntimeError("429 rate limit")) is False
    assert never_connected(RuntimeError("503 service unavailable")) is False


def test_the_predicate_reads_through_the_sdk_wrapper():
    """The failure never arrives bare — it arrives wrapped, and the wrapper's
    own message says only 'Request timed out.'"""
    assert never_connected(_wrapped_connect_failure()) is True


# --- the loop ----------------------------------------------------------------

def test_an_unreachable_host_is_given_up_on_in_seconds_not_minutes():
    p = _provider()
    # A 20s stand-in for the shipped 600s: the claim is "well short of the budget",
    # and a test that has to burn the real budget to prove it cannot be neutralised
    # affordably — which is the check that makes the test worth having.
    p.max_retry_seconds = 20
    calls = []

    async def always_unreachable():
        calls.append(time.monotonic())
        raise _wrapped_connect_failure()

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(p._retry_with_backoff(always_unreachable, _wrapped_connect_failure()))
    elapsed = time.monotonic() - started

    assert len(calls) == 1, f"one more attempt after the first failure, got {len(calls)}"
    assert elapsed < p.max_retry_seconds / 2, (
        f"gave up after {elapsed:.0f}s of a {p.max_retry_seconds}s budget — "
        f"that is the budget running out, not the route being recognised"
    )
    assert "never reached the host" in str(exc.value)
    # The cause has to survive into the message.
    assert "ConnectTimeout" in str(exc.value) or "Request timed out" in str(exc.value), (
        f"the raised message names no cause: {exc.value}"
    )


def _run_until_it_gives_up(raise_this, budget=20):
    """Drive the real loop against a call that always fails, and report how it ended."""
    p = _provider()
    p.max_retry_seconds = budget
    calls = []

    async def always_fails():
        calls.append(len(calls))
        raise raise_this()

    started = time.monotonic()
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(p._retry_with_backoff(always_fails, raise_this()))
    return len(calls), time.monotonic() - started, str(exc.value)


def test_a_busy_service_is_asked_more_often_than_a_dead_route():
    """The control, stated as a comparison rather than as a call count.

    An absolute count here would encode the ladder's arithmetic against whatever
    budget the test picked; the claim is a difference between the two failures.
    """
    dead, dead_elapsed, dead_msg = _run_until_it_gives_up(_wrapped_connect_failure)
    busy, busy_elapsed, busy_msg = _run_until_it_gives_up(
        lambda: RuntimeError("429 rate limit")
    )

    assert dead < busy, (
        f"a dead route ({dead} attempts) was asked as often as a busy service ({busy})"
    )
    assert dead_elapsed < busy_elapsed
    assert "never reached the host" in dead_msg
    assert "never reached the host" not in busy_msg, (
        "a service that answered was reported as unreachable"
    )


def test_two_connect_blips_split_by_an_answer_are_not_a_dead_route():
    """The run has to be consecutive: an answer in between resets it.

    40s rather than 20 because the ladder 3, 6, 12 makes three calls inside 21s,
    and three is the fewest this shape can be built from.
    """
    p = _provider()
    p.max_retry_seconds = 40
    seq = []

    async def blip_answer_blip():
        seq.append(len(seq))
        if len(seq) == 2:
            raise RuntimeError("503 service unavailable")
        raise _wrapped_connect_failure()

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(p._retry_with_backoff(blip_answer_blip, RuntimeError("503 service unavailable")))

    assert len(seq) >= 3, f"only {len(seq)} calls — the shape needs three"
    assert "never reached the host" not in str(exc.value), (
        "two blips split by an answer were read as a dead route"
    )
