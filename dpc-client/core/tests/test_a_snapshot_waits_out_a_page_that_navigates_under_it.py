"""A snapshot waits out a page that navigates under it.

A page that reloads itself (anti-bot scripts, a login bounce) destroys the JS
context in the middle of `page.evaluate`; Playwright answers "Execution
context was destroyed, most likely because of a navigation" and the tool used
to hand that to the agent on the first hit. The session now waits for the
document to settle and tries again, twice at most; any other error, and a dead
session, are returned as before.

The page is a fake that raises on a script of our choosing, so the retry logic
is driven without a browser. Playwright's real wording for the error is in
`_NAV_ERROR` below; Camoufox itself is NOT exercised here.
"""

import asyncio
import logging
import time
from unittest.mock import patch

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    PageKeptNavigating,
    _is_navigation_during_evaluate,
)

_NAV_ERROR = (
    "Page.evaluate: Execution context was destroyed, most likely because of "
    "a navigation"
)
_URL = "https://example.test/address_list"


class _Page:
    """Raises `errors` (consumed in order) from `evaluate`, then succeeds."""

    def __init__(self, errors, load_state_raises=None):
        self.url = _URL
        self._errors = list(errors)
        self._load_state_raises = load_state_raises
        self.evaluate_calls = 0
        self.settled = 0

    def evaluate(self, script, arg=None):
        self.evaluate_calls += 1
        if self._errors:
            err = self._errors.pop(0)
            if err is not None:
                raise err
        return {"nodes": []} if arg is not None else []

    def wait_for_load_state(self, state, timeout=None):
        assert state == "domcontentloaded"
        assert timeout is not None and timeout > 0
        self.settled += 1
        if self._load_state_raises is not None:
            raise self._load_state_raises

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture()
def _session(monkeypatch):
    monkeypatch.setattr(
        browser_mod, "_build_a11y_tree", lambda raw: ("the tree", {"@e1": {}}),
    )

    def make(page):
        session = AuthBrowser(agent_id="agent_test")
        session._page = page
        session._audit_action = lambda *a, **k: None  # type: ignore[method-assign]
        return session

    return make


def _retries(caplog):
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "hit a navigation" in r.getMessage()
    ]


def test_a_navigation_once_then_the_snapshot_comes_back(_session, caplog):
    page = _Page([Exception(_NAV_ERROR)])
    with caplog.at_level(logging.WARNING, logger=browser_mod.log.name):
        tree, refs = _session(page).a11y_snapshot()
    assert tree == "the tree" and refs == {"@e1": {}}
    assert page.evaluate_calls == 2
    assert page.settled == 1
    (record,) = _retries(caplog)
    message = record.getMessage()
    assert "attempt 1 of 3" in message and _URL in message


def test_a_page_that_never_stops_navigating_ends_in_a_clear_error(_session, caplog):
    page = _Page([Exception(_NAV_ERROR)] * 10)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=browser_mod.log.name):
        with pytest.raises(PageKeptNavigating) as raised:
            _session(page).a11y_snapshot()
    assert time.monotonic() - started < 5
    assert page.evaluate_calls == 3
    assert page.settled == 2
    assert len(_retries(caplog)) == 2
    text = str(raised.value)
    assert "kept navigating" in text and "3 snapshot attempts" in text
    assert "browser_screenshot" in text


def test_another_error_is_not_retried(_session):
    page = _Page([RuntimeError("Page.evaluate: SyntaxError: nope")])
    with pytest.raises(RuntimeError, match="SyntaxError"):
        _session(page).a11y_snapshot()
    assert page.evaluate_calls == 1
    assert page.settled == 0


def test_a_dead_session_is_not_taken_for_a_navigation(_session):
    dead = Exception(
        "Page.evaluate: Execution context was destroyed: "
        "Target page, context or browser has been closed"
    )
    page = _Page([dead])
    with pytest.raises(Exception, match="has been closed"):
        _session(page).a11y_snapshot()
    assert page.evaluate_calls == 1
    assert not _is_navigation_during_evaluate(dead)


def test_a_settle_that_times_out_does_not_hang_or_stop_the_retry(_session):
    page = _Page(
        [Exception(_NAV_ERROR)],
        load_state_raises=TimeoutError("Page.wait_for_load_state: Timeout 5000ms"),
    )
    started = time.monotonic()
    tree, _refs = _session(page).a11y_snapshot()
    assert time.monotonic() - started < 5
    assert tree == "the tree"
    assert page.evaluate_calls == 2 and page.settled == 1


def test_the_settle_wait_is_bounded_by_a_short_timeout():
    assert 0 < browser_mod._NAVIGATION_SETTLE_TIMEOUT_MS <= 10000
    assert browser_mod._NAVIGATION_RETRIES == 2


class _Ctx:
    class agent_root:
        name = "agent_test"


def _snapshot_tool(session):
    async def _direct(sess, verb, *args, **kw):
        kw.pop("_timeout", None)
        return getattr(sess, verb)(*args)

    async def _run():
        with patch.object(browser_mod, "_get_session_or_error", lambda _id: session), \
             patch.object(browser_mod, "_get_session_lock", lambda _id: asyncio.Lock()), \
             patch.object(browser_mod, "_run_in_session", _direct):
            return await browser_mod.browser_snapshot(_Ctx(), raw=True)

    return asyncio.run(_run())


def test_the_tool_returns_the_tree_after_a_retry(_session):
    session = _session(_Page([Exception(_NAV_ERROR)]))
    answer = _snapshot_tool(session)
    assert answer.startswith("the tree")
    assert "Snapshot failed" not in answer


def test_the_tool_names_the_cause_when_the_page_will_not_hold_still(_session):
    session = _session(_Page([Exception(_NAV_ERROR)] * 10))
    answer = _snapshot_tool(session)
    assert answer.startswith("⚠️ Snapshot failed: PageKeptNavigating")
    assert "kept navigating" in answer and "browser_screenshot" in answer
