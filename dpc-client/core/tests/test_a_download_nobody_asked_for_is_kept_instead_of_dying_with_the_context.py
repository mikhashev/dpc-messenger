"""A download that starts while no tool call is waiting is kept.

Playwright puts every download in a temp folder under a GUID and deletes it
when the context closes, so a file started by a hand click in the agent's
visible window left no file and no record.

Chromium against a local `http.server`, the way the other download tests do:
only a real engine fires the `download` event, and only a real one says
whether it still arrives while `expect_download` holds the same file.
Camoufox/Firefox, the production engine, is NOT exercised here.
"""

import asyncio
import json
import logging
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    DOWNLOAD_LEDGER_NAME,
    _new_context_kwargs,
    _unclaimed_download_dir,
)
from dpc_client_core.dpc_agent.tools.registry import ToolContext

_BOOK = b"%PDF-1.4\n" + b"a page nobody asked for\n" * 16

_PAGE = """<!doctype html>
<html><head><title>The page with the file on it</title></head><body>
  <a id="book" href="/book.pdf">Download the book</a>
  <a id="hostile" href="/hostile">A name that wants to be a path</a>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's own spelling
        if self.path == "/book.pdf":
            self._send(_BOOK, 'attachment; filename="book.pdf"')
            return
        if self.path == "/hostile":
            self._send(_BOOK, "attachment; filename=..\\..\\evil.exe")
            return
        body = _PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, body: bytes, disposition: str):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
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
def _agent_root(tmp_path, monkeypatch):
    """The sandbox, and the root the session resolves for itself — one
    folder, so a tool download and an unclaimed one land side by side."""
    root = tmp_path / "agents" / "agent_test"
    root.mkdir(parents=True)
    from dpc_client_core.dpc_agent import utils as agent_utils

    monkeypatch.setattr(
        agent_utils, "get_agent_root", lambda agent_id: tmp_path / "agents" / agent_id,
    )
    return root


def _ctx(agent_root: Path) -> ToolContext:
    return ToolContext(agent_root=agent_root, firewall=_Firewall())


@pytest.fixture()
def _session(_chromium, _server, _audit, _agent_root):
    context = _chromium.new_context(**_new_context_kwargs(headed=False))
    session = AuthBrowser(agent_id="agent_test")
    session._context = context
    session._page = context.new_page()
    session._watch_for_unclaimed_downloads(session._page)
    session._page.goto(f"{_server}/page.html")
    try:
        yield session
    finally:
        context.close()


def _step_to_completion(coro):
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("the tool awaited something needing a loop")


def _download(session, ctx, selector, **kwargs):
    async def _direct(sess, verb, *args, **kw):
        kw.pop("_timeout", None)
        return getattr(sess, verb)(*args, **kw)

    with patch.object(browser_mod, "_get_session_or_error", lambda _id: session), \
         patch.object(browser_mod, "_get_session_lock", lambda _id: asyncio.Lock()), \
         patch.object(browser_mod, "_run_in_session", _direct):
        return _step_to_completion(
            browser_mod.browser_download(ctx, selector, **kwargs)
        )


def _unclaimed(agent_root: Path) -> list[Path]:
    folder = agent_root / "downloads" / "unclaimed"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.name != DOWNLOAD_LEDGER_NAME)


def test_the_unclaimed_folder_sits_beside_the_downloads_the_agent_asked_for(
    _agent_root, tmp_path,
):
    folder = _unclaimed_download_dir("agent_test")
    assert folder == tmp_path / "agents" / "agent_test" / "downloads" / "unclaimed"


def test_a_download_no_tool_call_was_waiting_for_is_saved(
    _session, _agent_root, caplog,
):
    with caplog.at_level(logging.INFO, logger=browser_mod.log.name):
        _session._page.click("#book")
        _session._page.wait_for_timeout(1500)
    saved = _unclaimed(_agent_root)
    assert len(saved) == 1, saved
    assert saved[0].name == "book.pdf"
    assert saved[0].read_bytes() == _BOOK
    line = next(
        (r.getMessage() for r in caplog.records
         if "unclaimed download" in r.getMessage()),
        None,
    )
    assert line is not None, [r.getMessage() for r in caplog.records]
    assert "book.pdf" in line
    assert str(len(_BOOK)) in line
    assert "/book.pdf" in line


def test_the_name_the_site_chose_cannot_decide_where_it_lands(
    _session, _agent_root,
):
    _session._page.click("#hostile")
    _session._page.wait_for_timeout(1500)
    saved = _unclaimed(_agent_root)
    assert len(saved) == 1, saved
    assert "/" not in saved[0].name and "\\" not in saved[0].name
    assert saved[0].parent == (_agent_root / "downloads" / "unclaimed")


def test_each_unclaimed_file_gets_the_same_ledger_line_as_a_claimed_one(
    _session, _agent_root,
):
    _session._page.click("#book")
    _session._page.wait_for_timeout(1500)
    ledger = _agent_root / "downloads" / "unclaimed" / DOWNLOAD_LEDGER_NAME
    records = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    rec = records[0]
    for field in (
        "saved_at", "saved_path", "bytes", "sha256", "detected_type",
        "suggested_filename", "url", "page_url", "page_title", "note",
    ):
        assert field in rec, field
    assert rec["bytes"] == len(_BOOK)
    assert rec["detected_type"] == "pdf"
    assert rec["suggested_filename"] == "book.pdf"
    assert rec["url"].endswith("/book.pdf")
    assert rec["page_url"].endswith("/page.html")
    assert "unclaimed" in rec["note"]


def test_a_download_the_tool_asked_for_is_not_saved_a_second_time(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#book", timeout_seconds=10)
    assert (_agent_root / "downloads" / "book.pdf").read_bytes() == _BOOK, answer
    _session._page.wait_for_timeout(1000)
    assert _unclaimed(_agent_root) == []


def test_the_session_says_at_close_how_many_it_kept(
    _session, _agent_root, caplog, _audit,
):
    _session._page.click("#book")
    _session._page.wait_for_timeout(1500)
    _session._page.click("#book")
    _session._page.wait_for_timeout(1500)
    with caplog.at_level(logging.INFO, logger=browser_mod.log.name):
        _session.close()
    line = next(
        (r.getMessage() for r in caplog.records
         if "unclaimed" in r.getMessage() and "2" in r.getMessage()),
        None,
    )
    assert line is not None, [r.getMessage() for r in caplog.records]
    row = [r for r in _audit if r.get("action") == "close"][-1]
    assert row["unclaimed_downloads"] == 2


def test_the_listener_is_attached_by_the_open_path_itself(
    _chromium, _server, _agent_root, _audit, monkeypatch,
):
    """Not by the fixture: `_open` is what production calls, and a listener
    attached only in a test proves nothing. Chromium stands in for Camoufox;
    the rest of `_open` runs as it ships."""
    class _FakeCamoufox:
        def __init__(self, **kwargs):
            self.browser = _chromium

        def __enter__(self):
            return self.browser

        def __exit__(self, *args):
            return False

    module = types.ModuleType("camoufox.sync_api")
    module.Camoufox = _FakeCamoufox
    parent = types.ModuleType("camoufox")
    parent.sync_api = module
    monkeypatch.setitem(sys.modules, "camoufox", parent)
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", module)

    session = AuthBrowser(agent_id="agent_test")
    session._open()
    context = session._context
    try:
        session._page.goto(f"{_server}/page.html")
        session._page.click("#book")
        session._page.wait_for_timeout(1500)
        assert [p.name for p in _unclaimed(_agent_root)] == ["book.pdf"]
    finally:
        session.close()
        context.close()
