"""A click that never reaches the page says which layer is stuck.

Twelve of thirteen tool clicks ended at Playwright's "performing click
action" with nothing behind it — no request, no error, no line in the log.
The cause is a window the OS has stopped painting: Playwright's actionability
poll rides on requestAnimationFrame, which a minimised or off-screen window
delivers about once a second, while `visibilityState` still says "visible".

Two halves here. The routing on the frame count is driven through the real
threshold constant, because no test can minimise a headless window: a
threshold above any real rate makes every window a starved one, and a
threshold of zero makes none. The timeout half is reproduced with a
transparent overlay — the element is there and actionable-looking, the mouse
never reaches it — which is what a click that times out anyway looks like.

Chromium against a local `http.server`, the way the download tests do.
Camoufox/Firefox, the production engine, is NOT exercised here.
"""

import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    _new_context_kwargs,
)
from dpc_client_core.dpc_agent.tools.registry import ToolContext

_BOOK = b"%PDF-1.4\n" + b"one chapter, and then another\n" * 32

# A film over the whole page: every mouse click lands on it, every element
# under it stays visible, enabled and in the accessibility tree.
_COVERED = """<!doctype html>
<html><head><title>The covered page</title>
<style>#cover { position: fixed; inset: 0; z-index: 9; background: transparent; }</style>
</head><body>
  <div id="cover"></div>
  <button id="plain" type="button" onclick="document.title = 'pressed'">Press me</button>
  <form id="form" action="/landed.html" method="get">
    <button id="submit" type="submit">Send the form</button>
  </form>
  <a id="dl" href="/book.pdf" download>Download the book</a>
</body></html>
"""

_PLAIN = """<!doctype html>
<html><head><title>The open page</title></head><body>
  <button id="counted" type="button"
          onclick="window.__clicks = (window.__clicks || 0) + 1">Count me</button>
  <a id="dl" href="/book.pdf" download>Download the book</a>
</body></html>
"""

_LANDED = (
    "<!doctype html><html><head><title>Landed</title></head>"
    "<body>the form arrived</body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's own spelling
        if self.path == "/book.pdf":
            self._send(_BOOK, "application/octet-stream", 'attachment; filename="book.pdf"')
            return
        if self.path == "/plain.html":
            self._send(_PLAIN.encode("utf-8"), "text/html", None)
            return
        if self.path.startswith("/landed.html"):
            self._send(_LANDED.encode("utf-8"), "text/html", None)
            return
        self._send(_COVERED.encode("utf-8"), "text/html", None)

    def _send(self, body: bytes, content_type: str, disposition):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def _server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def _chromium():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - no browser extra
        pytest.skip(f"playwright not installed: {exc}")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - no browser binary
            pytest.skip(f"no chromium binary for playwright: {exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture()
def _audit(monkeypatch):
    rows: list[dict] = []
    from dpc_client_core import web_auth

    monkeypatch.setattr(web_auth, "log_browser_action", lambda **f: rows.append(f))
    return rows


class _Firewall:
    def get_extended_write_enabled(self, profile_name=None):
        return True

    def get_extended_read_enabled(self, profile_name=None):
        return True

    def is_extended_path_allowed(self, path, require_write=False, profile_name=None):
        return False


@pytest.fixture()
def _agent_root(tmp_path):
    root = tmp_path / "agents" / "agent_test"
    root.mkdir(parents=True)
    return root


def _ctx(agent_root: Path) -> ToolContext:
    return ToolContext(agent_root=agent_root, firewall=_Firewall())


@pytest.fixture()
def _session(_chromium, _server, _audit, monkeypatch):
    """An AuthBrowser driving a real Chromium page, on the covered page."""
    # The probes wait for a download that a live site would send in a moment;
    # here nothing is coming and the wait is the whole cost of the test.
    monkeypatch.setattr(browser_mod, "_CLICK_PROBE_DOWNLOAD_MS", 1000)
    context = _chromium.new_context(**_new_context_kwargs(headed=False))
    session = AuthBrowser(agent_id="agent_test")
    session._context = context
    session._page = context.new_page()
    session._page.goto(f"{_server}/covered.html")
    try:
        yield session
    finally:
        context.close()


def _step_to_completion(coro):
    """Run a coroutine whose every await completes without yielding — see the
    twin in the download test for why `asyncio.run` cannot be used here."""
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("the tool awaited something needing a loop")


def _direct_session(session):
    async def _direct(sess, verb, *args, **kw):
        kw.pop("_timeout", None)
        return getattr(sess, verb)(*args, **kw)

    return patch.object(
        browser_mod, "_get_session_or_error", lambda _id: session,
    ), patch.object(
        browser_mod, "_get_session_lock", lambda _id: asyncio.Lock(),
    ), patch.object(browser_mod, "_run_in_session", _direct)


def _click(session, ctx, selector, timeout=1000):
    a, b, c = _direct_session(session)
    with a, b, c:
        return _step_to_completion(
            browser_mod.browser_click(ctx, selector, timeout=timeout)
        )


def _download(session, ctx, selector, **kwargs):
    a, b, c = _direct_session(session)
    with a, b, c:
        return _step_to_completion(
            browser_mod.browser_download(ctx, selector, **kwargs)
        )


def _probe_lines(answer: str) -> list[str]:
    return [
        line.strip() for line in answer.splitlines()
        if line.strip().startswith(("evaluate:", "raf:", "js_click:"))
    ]


@pytest.fixture()
def _starved(monkeypatch):
    """Every window looks unpainted: a headless one cannot be minimised, and
    the threshold is the only input the routing reads."""
    monkeypatch.setattr(browser_mod, "_RAF_STARVED_BELOW", 10_000)


@pytest.fixture()
def _painted(monkeypatch):
    """No window can fall below zero frames, so none is ever starved."""
    monkeypatch.setattr(browser_mod, "_RAF_STARVED_BELOW", 0)


def test_the_facts_about_the_element_are_logged_before_the_click_is_tried(
    _session, _agent_root, caplog, _painted,
):
    """Agent-agnostic facts, at INFO, before the click that may say nothing."""
    with caplog.at_level(logging.INFO, logger=browser_mod.log.name):
        _click(_session, _ctx(_agent_root), "#plain")
    line = next(
        (r.getMessage() for r in caplog.records if "about to click" in r.getMessage()),
        None,
    )
    assert line is not None, [r.getMessage() for r in caplog.records]
    assert "#plain" in line
    assert "button" in line
    assert "complete" in line  # document.readyState
    assert "covered.html" in line
    assert f"in {browser_mod._RAF_SAMPLE_MS}ms" in line
    frames = int(line.split("raf=")[1].split(" ")[0])
    assert frames > 0, line


def test_an_unpainted_window_takes_the_dom_event_and_the_answer_says_so(
    _session, _agent_root, _starved, caplog,
):
    """The overlay swallows every mouse click on this page, so a press that
    landed is a press that did not go through the mouse."""
    with caplog.at_level(logging.WARNING, logger=browser_mod.log.name):
        answer = _click(_session, _ctx(_agent_root), "#plain")
    assert _session._page.title() == "pressed"
    assert "as a DOM event" in answer
    assert "window not painted" in answer
    assert "minimised or off-screen" in answer
    assert "Restore it on screen" in answer
    assert any(
        "not being painted" in r.getMessage() for r in caplog.records
        if r.levelname == "WARNING"
    ), [r.getMessage() for r in caplog.records]


def test_a_dispatched_click_still_submits_a_form(
    _session, _agent_root, _starved,
):
    """The DOM event is not a lesser click: a submit button pressed this way
    sends the form, so nothing else is needed for a form."""
    _click(_session, _ctx(_agent_root), "#submit")
    _session._page.wait_for_url("**/landed.html*", timeout=5000)


def test_a_painted_window_clicks_the_ordinary_way_and_says_nothing_extra(
    _session, _agent_root, _server, _painted,
):
    _session._page.goto(f"{_server}/plain.html")
    answer = _click(_session, _ctx(_agent_root), "#counted")
    assert answer == "Clicked #counted"
    assert _session._page.evaluate("window.__clicks") == 1


def test_the_delivery_reaches_the_audit_row(
    _session, _agent_root, _audit, _starved,
):
    _click(_session, _ctx(_agent_root), "#plain")
    row = [r for r in _audit if r.get("action") == "click"][-1]
    assert row["result"] == "ok"
    assert row["delivery"] == browser_mod._CLICK_DELIVERY_EVENT
    assert isinstance(row["raf"], int)


def test_a_click_the_mouse_never_delivers_reports_the_probes(
    _session, _agent_root, _painted,
):
    answer = _click(_session, _ctx(_agent_root), "#plain")
    assert answer.startswith("⚠️ Click failed")
    assert "TimeoutError" in answer
    lines = _probe_lines(answer)
    assert len(lines) == 3, answer
    # The main thread answers, the window IS being painted, and a click
    # dispatched from JS gets through: the mouse alone was stuck.
    assert lines[0] == "evaluate: passed"
    assert lines[1].startswith("raf: ")
    assert "starved" not in lines[1]
    assert lines[2] == "js_click: passed"
    # The rescue really pressed the button — the answer says it acts.
    assert _session._page.title() == "pressed"
    assert "also an attempt" in answer


def test_the_click_answer_stays_bounded_and_keeps_the_probes_on_own_lines(
    _session, _agent_root, _painted,
):
    answer = _click(_session, _ctx(_agent_root), "#plain")
    # Playwright's own message carries a call log dozens of lines long.
    assert len(answer.splitlines()) <= 6, answer
    assert "Call log" not in answer
    assert "performing click action" not in answer


def test_the_probe_outcomes_reach_the_audit_row(
    _session, _agent_root, _audit, _painted,
):
    _click(_session, _ctx(_agent_root), "#plain")
    row = [r for r in _audit if r.get("action") == "click"][-1]
    assert row["result"] == "failed"
    assert row["probes"][0] == "evaluate: passed"
    assert row["probes"][-1] == "js_click: passed"


def test_the_rescue_submits_a_form_without_a_probe_of_its_own(
    _session, _agent_root, _painted,
):
    """`el.click()` on a submit button submits, which is why there is no
    separate requestSubmit probe to risk a second navigation."""
    answer = _click(_session, _ctx(_agent_root), "#submit")
    assert _probe_lines(answer)[-1] == "js_click: passed"
    _session._page.wait_for_url("**/landed.html*", timeout=5000)


def test_a_ref_the_snapshot_no_longer_answers_is_refused_without_probes(
    _session, _agent_root, _audit, _painted,
):
    """A stale ref is not a stuck click: nothing was clicked, so nothing is
    probed, and the audit row still records the refusal."""
    answer = _click(_session, _ctx(_agent_root), "@e9999")
    assert "unknown ref" in answer
    assert _probe_lines(answer) == []
    row = [r for r in _audit if r.get("action") == "click"][-1]
    assert row["result"] == "failed"
    assert not row.get("probes")


def test_a_download_dispatched_at_an_unpainted_window_still_lands(
    _session, _agent_root, _audit, _starved,
):
    """The dispatch happens inside the same `expect_download`, so the file
    it starts is caught rather than lost with the context."""
    answer = _download(_session, _ctx(_agent_root), "#dl", timeout_seconds=10)
    saved = _agent_root / "downloads" / "book.pdf"
    assert saved.is_file(), answer
    assert saved.read_bytes() == _BOOK
    assert "DOM event" in answer
    assert "minimised or off-screen" in answer
    rows = [r for r in _audit if r.get("action") == "download"]
    assert rows[-1]["result"] == "ok"


def test_a_download_the_rescue_starts_is_saved_and_the_answer_names_it(
    _session, _agent_root, _audit, _painted,
):
    """The rescue acts, so a probe that works IS how the file arrives — and a
    download nobody is waiting for dies with the context."""
    answer = _download(_session, _ctx(_agent_root), "#dl", timeout_seconds=1)
    saved = _agent_root / "downloads" / "book.pdf"
    assert saved.is_file(), answer
    assert saved.read_bytes() == _BOOK
    assert "js_click" in answer
    assert "probe" in answer
    rows = [r for r in _audit if r.get("action") == "download"]
    assert rows[-1]["result"] == "ok"


def test_a_click_that_reached_the_page_and_downloaded_nothing_is_not_reclicked(
    _session, _agent_root, _server, _painted,
):
    """The probes answer a click that never landed. A click that landed and
    started no download must not press the element a second time — a site
    that meters downloads charges for the second press."""
    _session._page.goto(f"{_server}/plain.html")
    answer = _download(_session, _ctx(_agent_root), "#counted", timeout_seconds=1)
    assert "No download started" in answer
    assert _probe_lines(answer) == [], answer
    assert _session._page.evaluate("window.__clicks") == 1


class _FakeLocator:
    """An element that answers the frame count it is built with, and records
    which of the two deliveries was asked for."""

    def __init__(self, raf: int):
        self.raf = raf
        self.clicks = 0
        self.dispatched: list[str] = []

    def evaluate(self, expression, arg=None, timeout=None):
        return {"tag": "button", "ready": "complete", "raf": self.raf}

    def click(self, timeout=None):
        self.clicks += 1

    def dispatch_event(self, type, timeout=None):
        self.dispatched.append(type)


class _FakePage:
    url = "http://example.test/page"

    def title(self):
        return "a page"


@pytest.mark.parametrize(
    "raf,dispatched,clicked",
    [(0, ["click"], 0), (1, ["click"], 0), (9, ["click"], 0), (10, [], 1), (60, [], 1)],
)
def test_the_frame_count_alone_decides_how_the_click_is_delivered(
    _audit, raf, dispatched, clicked,
):
    assert browser_mod._RAF_STARVED_BELOW == 10
    session = AuthBrowser(agent_id="agent_test")
    session._page = _FakePage()
    locator = _FakeLocator(raf)
    session._resolve_ref = lambda ref: locator
    outcome = session.click("#anything")
    assert locator.dispatched == dispatched
    assert locator.clicks == clicked
    assert outcome["raf"] == raf
    assert outcome["starved"] is bool(dispatched)


def test_a_frame_count_that_never_arrived_leaves_the_ordinary_click(_audit):
    """A page that cannot be asked is not a page declared unpainted."""
    session = AuthBrowser(agent_id="agent_test")
    session._page = _FakePage()
    locator = _FakeLocator(0)
    locator.evaluate = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("gone"))
    session._resolve_ref = lambda ref: locator
    outcome = session.click("#anything")
    assert locator.clicks == 1
    assert locator.dispatched == []
    assert outcome["starved"] is False


def test_a_stuck_click_with_no_download_still_reports_its_probes(
    _session, _agent_root, _painted,
):
    answer = _download(_session, _ctx(_agent_root), "#plain", timeout_seconds=1)
    assert "No download started" in answer
    lines = _probe_lines(answer)
    assert lines[0] == "evaluate: passed"
    assert lines[1].startswith("raf: ")
    assert lines[2] == "js_click: passed"
