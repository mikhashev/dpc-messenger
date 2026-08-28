"""A window the person closed must take its processes with it.

The browser cannot tell us it was closed: the sync Playwright client
dispatches events only while its thread is inside a call, and a session
whose agent has finished parks on its queue. So the housekeeping asks,
and these tests hold the two properties that asking has to have — it
recognises only a gone browser, and it does not itself look like use.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from dpc_client_core.dpc_agent.tools import browser as B


class FakePage:
    """A page that answers, or refuses in a named way."""

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.asked = 0

    def title(self) -> str:
        self.asked += 1
        if self._exc is not None:
            raise self._exc
        return "a title"


def _session(page, headed: bool = True, agent_id: str = "agent_probe"):
    ab = B.AuthBrowser(agent_id=agent_id, headed=headed)
    ab._page = page
    return ab


@pytest.fixture(autouse=True)
def clean_registry():
    before = dict(B._active_browser_sessions)
    B._active_browser_sessions.clear()
    yield
    B._active_browser_sessions.clear()
    B._active_browser_sessions.update(before)


def test_a_window_that_answers_is_not_gone():
    ab = _session(FakePage())
    assert ab.window_is_gone() is False


def test_a_closed_window_is_gone():
    ab = _session(
        FakePage(Exception("Target page, context or browser has been closed"))
    )
    assert ab.window_is_gone() is True


def test_a_timeout_is_not_a_closed_window():
    """The property the whole check rests on: a slow page is not a gone
    one. Treating every failure as gone would close the browser out from
    under someone whose page merely took too long."""
    ab = _session(FakePage(Exception("Timeout 10000ms exceeded")))
    assert ab.window_is_gone() is False


def test_a_session_with_no_page_is_gone():
    ab = _session(None)
    assert ab.window_is_gone() is True


def test_the_sweep_releases_a_headed_session_whose_window_went_away():
    ab = _session(
        FakePage(Exception("Target page, context or browser has been closed")),
        agent_id="agent_gone",
    )
    closed = []
    ab.close = lambda: closed.append(True)  # type: ignore[method-assign]
    B._active_browser_sessions["agent_gone"] = ab

    released = asyncio.run(B.sweep_closed_windows())

    assert released == 1
    assert closed == [True]
    assert "agent_gone" not in B._active_browser_sessions


def test_the_sweep_leaves_a_live_window_alone():
    ab = _session(FakePage(), agent_id="agent_live")
    closed = []
    ab.close = lambda: closed.append(True)  # type: ignore[method-assign]
    B._active_browser_sessions["agent_live"] = ab

    released = asyncio.run(B.sweep_closed_windows())

    assert released == 0
    assert closed == []
    assert B._active_browser_sessions["agent_live"] is ab


def test_the_sweep_ignores_a_headless_session():
    """The fetch browser has no window at all — its trigger is the end of
    the agent run that opened it, and asking it about a window would
    answer a question nobody asked."""
    page = FakePage(Exception("Target page, context or browser has been closed"))
    ab = _session(page, headed=False, agent_id="agent_fetch")
    B._active_browser_sessions["agent_fetch"] = ab

    released = asyncio.run(B.sweep_closed_windows())

    assert released == 0
    assert page.asked == 0
    assert B._active_browser_sessions["agent_fetch"] is ab


def test_the_probe_does_not_count_as_use():
    """Every call through `_run_in_session` normally stamps the session as
    used. The probe runs every half minute on its own initiative, so if it
    stamped too, no session would ever look idle again and the idle sweep
    would quietly stop working."""
    ab = _session(FakePage(), agent_id="agent_untouched")
    B._active_browser_sessions["agent_untouched"] = ab
    ab._last_activity = time.monotonic() - 9999
    before = ab._last_activity

    asyncio.run(B.sweep_closed_windows())

    assert ab._last_activity == before


class SlowPage:
    """A window that does not answer — busy, wedged, mid-navigation."""

    def title(self) -> str:
        time.sleep(2)
        return "eventually"


def test_a_wedged_session_neither_holds_up_the_sweep_nor_gets_closed(monkeypatch):
    """Sessions are asked one after another. Without its own short
    deadline the probe would inherit the two minutes an agent's own call
    is allowed, and one wedged browser would hide every other closed
    window for that long. And a browser that does not answer is not a
    closed one: closing it would take a live page away from somebody."""
    monkeypatch.setattr(B, "WINDOW_PROBE_TIMEOUT_SECONDS", 0.2)
    wedged = _session(SlowPage(), agent_id="agent_wedged")
    gone = _session(
        FakePage(Exception("Target page, context or browser has been closed")),
        agent_id="agent_after_it",
    )
    gone.close = lambda: None  # type: ignore[method-assign]
    B._active_browser_sessions["agent_wedged"] = wedged
    B._active_browser_sessions["agent_after_it"] = gone

    started = time.monotonic()
    released = asyncio.run(B.sweep_closed_windows())
    elapsed = time.monotonic() - started

    assert released == 1, "the closed window behind the wedged one was missed"
    assert elapsed < 1.5, f"the wedged session held the sweep for {elapsed:.1f}s"
    assert B._active_browser_sessions.get("agent_wedged") is wedged


def test_a_disconnect_releases_the_browser_instead_of_dropping_the_handles():
    """The disconnect handler used to empty the three registries and stop
    there — which removed the only way anything could reach the session
    afterwards, leaving the Camoufox context manager unexited and its
    driver subprocess alive."""
    ab = _session(FakePage(), agent_id="agent_disconnect")
    B._active_browser_sessions["agent_disconnect"] = ab
    ran = threading.Event()
    ab.close = lambda: ran.set()  # type: ignore[method-assign]
    ab._get_executor()

    ab._on_browser_disconnected()

    assert ran.wait(timeout=5), "disconnect dropped the session without closing it"
    assert "agent_disconnect" not in B._active_browser_sessions
