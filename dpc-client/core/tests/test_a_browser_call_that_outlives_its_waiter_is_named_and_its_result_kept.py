"""A browser call that outlives its waiter must not silently hold the channel.

Calls cannot be cancelled once running. These tests use a fake session whose
methods are slow, on the real pinned thread, and hold: the next call is told
which call is busy, collect ends itself on its budget, a late result is
kept and surfaced, and fast calls behave as before.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.tools import browser as B


def _session(agent_id: str = "agent_late"):
    ab = B.AuthBrowser(agent_id=agent_id, headed=True)
    ab._page = SimpleNamespace(url="https://example.test/", is_closed=lambda: False)
    return ab


@pytest.fixture
def session():
    ab = _session()
    B._active_browser_sessions[ab._agent_id] = ab
    yield ab
    B._active_browser_sessions.pop(ab._agent_id, None)
    ab._shutdown_executor()


def test_fast_calls_are_unchanged(session):
    session.quick = lambda x: x * 2
    assert asyncio.run(B._run_in_session(session, "quick", 21)) == 42
    assert session._executor.current is None
    assert session._late_results == []


def test_a_slow_call_is_reported_busy_to_the_next_call_by_name(session):
    release = threading.Event()
    session.slow = lambda: (release.wait(10), "done")[1]
    session.quick = lambda: "fast"

    async def go():
        with pytest.raises(B.SessionCallAbandoned) as first:
            await B._run_in_session(session, "slow", _timeout=0.2)
        t0 = time.monotonic()
        with pytest.raises(B.SessionBusyError) as second:
            await B._run_in_session(session, "quick", _timeout=5)
        return first.value, second.value, time.monotonic() - t0

    try:
        first, second, took = asyncio.run(go())
    finally:
        release.set()
    assert took < 1.0  # refused at once, not queued behind the slow call
    msg = str(second)
    assert msg.startswith("browser session busy: browser_slow started ")
    assert "s ago) is still running; this call was not executed" in msg
    assert isinstance(second, TimeoutError)
    assert "browser_slow" in str(first) and "still running" in str(first)


def test_the_window_probe_does_not_call_a_busy_channel_a_failed_window(session, caplog):
    release = threading.Event()
    session.slow = lambda: release.wait(10)
    session.window_is_gone = lambda: False

    async def go():
        with pytest.raises(B.SessionCallAbandoned):
            await B._run_in_session(session, "slow", _timeout=0.2)
        return await B.sweep_closed_windows()

    import logging
    with caplog.at_level(logging.DEBUG, logger=B.log.name):
        try:
            released = asyncio.run(go())
        finally:
            release.set()
    assert released == 0
    text = caplog.text
    assert "window probe skipped" in text and "browser session busy" in text
    assert "window probe failed" not in text


def test_a_late_result_is_kept_and_surfaced_once(session):
    release = threading.Event()
    session.slow = lambda: (release.wait(10), {"items": [{"text": "a", "href": "h1"}],
                                               "total": 1, "scrolls_done": 4})[1]

    async def go():
        with pytest.raises(B.SessionCallAbandoned):
            await B._run_in_session(session, "slow", _timeout=0.2)
        release.set()
        for _ in range(100):
            if session._late_results:
                break
            await asyncio.sleep(0.05)

    asyncio.run(go())
    assert len(session._late_results) == 1
    notice = session._take_late_notice()
    assert "the previous browser_slow (call " in notice
    assert "finished late" in notice and "1 items after 4 scrolls" in notice
    assert "result available" in notice and "h1" in notice
    assert session._take_late_notice() == ""  # reported once


def test_the_next_tool_answer_carries_the_late_notice(session, tmp_path):
    session._keep_late_result(B._CallRecord("collect"), {"total": 7, "scrolls_done": 2})
    session.quick = lambda: "ok"

    @B._reports_late_results
    async def browser_fake(ctx):
        return "answer"

    ctx = SimpleNamespace(agent_root=tmp_path / session._agent_id)
    out = asyncio.run(browser_fake(ctx))
    assert "the previous browser_collect" in out and "7 items after 2 scrolls" in out
    assert out.endswith("answer")
    assert asyncio.run(browser_fake(ctx)) == "answer"


def _collect_session(pause_s: float = 0.0):
    ab = _session("agent_collect")
    ab._audit_action = lambda *a, **k: None
    counter = {"n": 0}

    def evaluate(js, arg=None):
        return {"items": [{"text": f"item{counter['n']}"}], "found": 1, "matched": 1}

    ab._page.evaluate = evaluate

    def scroll(direction, amount):
        counter["n"] += 1
        time.sleep(pause_s)

    ab.scroll = scroll
    return ab


def test_collect_stops_on_its_time_budget_and_returns_partial():
    ab = _collect_session(pause_s=0.05)
    result = ab.collect("#c", ".i", ["text"], max_scrolls=1000,
                        scroll_pause_ms=10, time_budget_s=0.3)
    assert result["stop_reason"] == "time_budget"
    assert 0 < result["scrolls_done"] < 1000
    assert result["total"] == len(result["items"]) > 0


def test_collect_stops_when_its_call_is_abandoned():
    ab = _collect_session(pause_s=0.02)
    ab._executor = SimpleNamespace(current=SimpleNamespace(abandoned=False))

    def abandon_after_three(direction, amount):
        ab._executor.current.abandoned = ab._executor.current.__dict__.setdefault("n", 0) >= 3
        ab._executor.current.n += 1

    ab.scroll = abandon_after_three
    result = ab.collect("#c", ".i", ["text"], max_scrolls=1000, scroll_pause_ms=1)
    assert result["stop_reason"] == "abandoned"
    assert result["scrolls_done"] <= 5


def test_the_collect_answer_names_the_budget_stop(session, tmp_path):
    session.collect = lambda *a, **k: {
        "items": [{"text": "x", "href": "h"}], "total": 1, "scrolls_done": 12,
        "stop_reason": "time_budget", "time_budget_s": 90.0, "max_scrolls": 30,
        "consecutive_empty": 0,
    }
    ctx = SimpleNamespace(agent_root=tmp_path / session._agent_id)
    out = asyncio.run(B.browser_collect(ctx, "#c", ".i", ["text"]))
    assert "INCOMPLETE: stopped after 12 scrolls on the 90s time budget; partial: 1 items" in out
