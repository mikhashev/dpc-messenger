"""The idle reaper must read the window, not only the agent.

A headed browser is opened for one reason: a person has to sign in with
their hands. For as long as that takes, the agent makes no calls — so a
reaper that watches only the agent's clock closes the window with the
2FA prompt still on screen. These tests hold both halves of the fix: a
window whose page is talking survives the threshold, and a window that
nothing at all is doing is still collected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import types

import pytest

from dpc_client_core.dpc_agent.tools import browser as B

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


def _request(url: str, *, method: str = "GET", resource_type: str = "xhr"):
    return types.SimpleNamespace(
        url=url, method=method, resource_type=resource_type,
        is_navigation_request=lambda: False, frame=None,
    )


def _visible_passthrough_rows(home, agent_id: str) -> list[dict]:
    """The trail rows a visible window leaves in that agent's audit log."""
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("action")
            == B.AuthBrowser.GATE_ACTION_VISIBLE_PASSTHROUGH]


class _Page:
    """Enough page for `window_is_gone` and for `navigate`."""

    url = "https://example/"

    def __init__(self) -> None:
        self.goto_calls: list[str] = []

    def title(self) -> str:
        return "a title"

    def goto(self, url, **kw):
        self.goto_calls.append(url)
        return types.SimpleNamespace(status=200)


def _headed_session(agent_id: str, *, domains: list[str] | None = None):
    ab = B.AuthBrowser(agent_id=agent_id, domains=domains or [], headed=True)
    ab._page = _Page()
    return ab


@pytest.fixture(autouse=True)
def clean_registry():
    before = dict(B._active_browser_sessions)
    B._active_browser_sessions.clear()
    yield
    B._active_browser_sessions.clear()
    B._active_browser_sessions.update(before)


def _stale(session) -> None:
    """No agent call since well before the threshold."""
    session._last_activity = time.monotonic() - B.IDLE_TIMEOUT_SECONDS - 600


def _reap(session, agent_id: str) -> tuple[int, list]:
    closed_marks: list = []
    session.close = lambda: closed_marks.append(True)  # type: ignore[method-assign]
    B._active_browser_sessions[agent_id] = session
    n = asyncio.run(B.cleanup_idle_browser_sessions())
    return n, closed_marks


def test_a_visible_window_survives_a_person_signing_in(vault_home):  # noqa: F811
    """The falsifier for the bug: no agent call for longer than the
    threshold, but the page is making requests — which is what a person
    typing into a login form looks like from this side.

    Takes `vault_home` though it never reads it: `_note_visible_request`
    appends an audit row, and without a redirected home that row lands in
    the operator's own ~/.dpc."""
    ab = _headed_session("agent_signing_in")
    _stale(ab)
    ab._note_visible_request(_request("https://accounts.example/2fa/poll"))

    closed, marks = _reap(ab, "agent_signing_in")

    assert closed == 0, "the window was closed under the person"
    assert marks == []
    assert B._active_browser_sessions.get("agent_signing_in") is ab


def test_a_visible_window_nothing_is_doing_is_still_reaped():
    """The case the sweep exists for: a window a dead run left behind.
    Neither clock has moved, so it goes."""
    ab = _headed_session("agent_abandoned")

    _stale(ab)

    closed, marks = _reap(ab, "agent_abandoned")

    assert closed == 1
    assert marks == [True]
    assert "agent_abandoned" not in B._active_browser_sessions


def test_an_age_older_than_the_hosts_uptime_is_still_that_age():
    """`time.monotonic()` counts from boot. On a runner up for five minutes
    "no page event" (0.0) used to floor the agent's clock, so a call 40
    minutes ago read as 5 minutes idle and nothing was reaped."""
    uptime = 300.0
    session = types.SimpleNamespace(
        _last_activity=uptime - 2400.0, _last_page_event=0.0,
    )

    assert B._session_idle_seconds(session, uptime) == 2400.0


def test_a_page_event_ages_out_like_an_agent_call():
    """A page event does not make a window immortal — an old one is as
    idle as an old call, or the reaper would keep every window that ever
    loaded a page."""
    ab = _headed_session("agent_long_quiet")
    _stale(ab)
    ab._last_page_event = time.monotonic() - B.IDLE_TIMEOUT_SECONDS - 60

    closed, marks = _reap(ab, "agent_long_quiet")

    assert closed == 1
    assert marks == [True]


def test_a_page_request_is_not_recorded_as_an_agent_call(vault_home):  # noqa: F811
    """Two clocks, not one: the window probe's `_touch=False` contract is
    written about `_last_activity`, and folding page traffic into it would
    leave nobody able to say which of the two went quiet."""
    ab = _headed_session("agent_two_clocks")
    _stale(ab)
    agent_clock = ab._last_activity

    ab._note_visible_request(_request("https://accounts.example/2fa/poll"))

    assert ab._last_activity == agent_clock
    assert ab._last_page_event > agent_clock


def test_a_page_event_is_noted_even_where_nothing_is_audited(vault_home):  # noqa: F811
    """An unscoped session audits no passthrough rows at all. Whether the
    window is in use is a different question from what gets written down,
    so the stamp must survive the audit filter — and the filter has to hold
    in the other direction too, or a session that asked for no scope starts
    writing down every address the person visited in their own window."""
    ab = _headed_session("agent_open_scope")
    assert ab._open_scope is True

    ab._note_visible_request(_request("https://anywhere.example/ping"))

    assert ab._last_page_event > 0.0
    assert _visible_passthrough_rows(vault_home, "agent_open_scope") == []


def test_the_reaper_says_which_clock_ran_out(caplog):
    """Instrumentation first: without both ages, the window, and where it
    was, the next window dies as silently as the one that prompted this."""
    ab = _headed_session("agent_logged")
    _stale(ab)
    ab._last_known_url = "https://accounts.example/login"

    with caplog.at_level(logging.INFO, logger=B.log.name):
        _reap(ab, "agent_logged")

    line = next(r.getMessage() for r in caplog.records
                if "Closing idle" in r.getMessage())
    assert "last agent call" in line
    assert "last page event never" in line
    assert "headed=True" in line
    assert "https://accounts.example/login" in line


def test_a_navigation_leaves_the_url_the_reaper_reports(vault_home):  # noqa: F811
    """`self._page.url` cannot be read from the reaper's thread, so the
    window's address has to be kept somewhere plain as it is set."""
    ab = _headed_session("agent_nav", domains=[TEST_DOMAIN])
    ab._wait_for_content_stable = lambda: None  # type: ignore[method-assign]
    ab.a11y_snapshot = lambda: ("", {})  # type: ignore[method-assign]

    ab.navigate(f"https://{TEST_DOMAIN}/login")

    assert ab._last_known_url == f"https://{TEST_DOMAIN}/login"
