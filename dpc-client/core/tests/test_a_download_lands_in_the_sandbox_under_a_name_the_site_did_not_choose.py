"""A download the agent clicks lands inside its sandbox, under a name the
site did not choose, and the answer says what the bytes are.

Chromium against a local `http.server`, the way the snapshot tests do: only a
real engine says whether a context accepts a download, what
`suggested_filename` holds after the engine has had its say, and what
`expect_download` does when the click merely navigates. Camoufox/Firefox, the
production engine, is NOT exercised here.

Chromium sanitizes `suggested_filename` itself, so the exact mapping for a
hostile `Content-Disposition` is asserted against `_safe_download_name`
directly; through the browser the assertions are the invariants that hold on
any engine — inside the directory, no separators, no device name, bounded.
"""

import hashlib
import inspect
import json
import math
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    DOWNLOAD_LEDGER_NAME,
    _DOWNLOAD_DIR_DEFAULT,
    _DOWNLOAD_NAME_MAX,
    _WINDOWS_DEVICE_NAMES,
    _new_context_kwargs,
    _safe_download_name,
    _sniff_file_type,
    _type_contradicts_extension,
    _unique_download_path,
)
from dpc_client_core.dpc_agent.tools.registry import ToolContext

_BOOK = b"%PDF-1.4\n" + b"chapter one, and it goes on\n" * 64
_LONG_STEM = "a" * 400

# What a site answers with when the click reached a login wall, not the file.
_WALL = b"<!DOCTYPE html>\n<html><body>Please sign in to download.</body></html>"

_ROUTES: dict[str, tuple[str, bytes]] = {
    "/book.pdf": ('attachment; filename="book.pdf"', _BOOK),
    "/slash": ('attachment; filename="../../evil.exe"', b"slash body"),
    "/backslash": ("attachment; filename=..\\..\\evil.exe", b"backslash body"),
    "/device": ('attachment; filename="CON.txt"', b"device body"),
    "/long": (f'attachment; filename="{_LONG_STEM}.pdf"', b"long body"),
    "/nameless": ('attachment; filename=""', b"nameless body"),
    "/wall.pdf": ('attachment; filename="wall.pdf"', _WALL),
    "/big.bin": ('attachment; filename="big.bin"', b"x" * 4096),
}

_PAGE = """<!doctype html>
<html><head><title>The download page</title></head><body>
  <a id="book" href="/book.pdf">Download the book</a>
  <a id="slash" href="/slash">slash traversal</a>
  <a id="backslash" href="/backslash">backslash traversal</a>
  <a id="device" href="/device">device name</a>
  <a id="long" href="/long">long name</a>
  <a id="nameless" href="/nameless">no name at all</a>
  <a id="wall" href="/wall.pdf">a wall under a pdf name</a>
  <a id="big" href="/big.bin">big</a>
  <a id="plain" href="/other.html">An ordinary link</a>
  <button id="inert" type="button">A button that starts nothing</button>
</body></html>
"""

_OTHER = (
    "<!doctype html><html><head><title>Somewhere else</title></head>"
    "<body>ordinary page</body></html>"
)

# A host that is not the server's, and never navigated to: what is under test
# is which links the page carries, not what they answer.
_FILE_HOST = "http://files.example.com"
_FALLBACK_TEXT = "If the download did not start, use this link"
_LONG_QUERY = "q=" + "z" * 400

_FALLBACK_PAGE = f"""<!doctype html>
<html><head><title>The button and its fallback</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="{_FILE_HOST}/book.pdf">{_FALLBACK_TEXT}</a>
  <a href="/other.html">An ordinary link</a>
  <a href="javascript:void(0)">runs a script</a>
  <a href="mailto:someone@example.com">write to us</a>
  <a href="ftp://ftp.example.org/book.pdf">an ftp mirror</a>
</body></html>
"""

_SAME_HOST_PAGE = """<!doctype html>
<html><head><title>Its own links only</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="/other.html">An ordinary link</a>
  <a href="/book.pdf">Download the book</a>
</body></html>
"""

# Seven hosts and one repeat of the first: the repeat must not take a slot,
# or the fifth host named would be the fourth.
_MANY_HOSTS_PAGE = """<!doctype html>
<html><head><title>Many mirrors</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="http://mirror1.example.com/book.pdf">mirror one</a>
  <a href="http://mirror1.example.com/book.pdf">mirror one again</a>
""" + "".join(
    f'  <a href="http://mirror{i}.example.com/book.pdf">mirror {i}</a>\n'
    for i in range(2, 8)
) + """</body></html>
"""

_LONG_QUERY_PAGE = f"""<!doctype html>
<html><head><title>One very long href</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="{_FILE_HOST}/book.pdf?{_LONG_QUERY}">the long one</a>
</body></html>
"""

_LONG_TEXT = "word " * 80

_LONG_TEXT_PAGE = f"""<!doctype html>
<html><head><title>One very long link text</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="{_FILE_HOST}/book.pdf">{_LONG_TEXT}</a>
</body></html>
"""


_LINK_PAGES = {
    "/fallback.html": _FALLBACK_PAGE,
    "/samehost.html": _SAME_HOST_PAGE,
    "/manyhosts.html": _MANY_HOSTS_PAGE,
    "/longquery.html": _LONG_QUERY_PAGE,
    "/longtext.html": _LONG_TEXT_PAGE,
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's own spelling
        if self.path in ("/", "/page.html"):
            self._send(_PAGE.encode("utf-8"), "text/html", None)
            return
        if self.path == "/other.html":
            self._send(_OTHER.encode("utf-8"), "text/html", None)
            return
        page = _LINK_PAGES.get(self.path)
        if page is not None:
            self._send(page.encode("utf-8"), "text/html", None)
            return
        route = _ROUTES.get(self.path)
        if route is None:
            self.send_error(404)
            return
        disposition, body = route
        self._send(body, "application/octet-stream", disposition)

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
    """A local origin: `page.set_content` leaves relative hrefs pointing
    nowhere, and the headers are what is under test."""
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
    """A real browser, or a clean skip: playwright is an extra, and an
    installed package still has no binary until `playwright install` ran."""
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
    """The audit rows in memory: the real writer appends under the user's own
    ~/.dpc, which a test may not touch."""
    rows: list[dict] = []
    from dpc_client_core import web_auth

    monkeypatch.setattr(web_auth, "log_browser_action", lambda **f: rows.append(f))
    return rows


class _Firewall:
    """The two questions the resolver asks about an absolute path: is extended
    write on for this agent, and is this path one of its grants."""

    def __init__(self, granted=()):
        self._granted = [Path(p).resolve() for p in granted]

    def get_extended_write_enabled(self, profile_name=None):
        return True

    def get_extended_read_enabled(self, profile_name=None):
        return True

    def is_extended_path_allowed(self, path, require_write=False, profile_name=None):
        target = Path(path)
        return any(
            root == target or root in target.parents for root in self._granted
        )


def _ctx(agent_root: Path, granted=()) -> ToolContext:
    """A real ToolContext with a faked grant list.

    The download path resolves its directory through `_resolve_file_path`, and
    a test that re-implemented sandbox and grant checks here would decide the
    refusals itself and prove nothing about that resolver. Only Agent
    Permissions → Extended Paths is faked; the rest is production code.
    """
    return ToolContext(agent_root=agent_root, firewall=_Firewall(granted))


@pytest.fixture()
def _agent_root(tmp_path):
    root = tmp_path / "agents" / "agent_test"
    root.mkdir(parents=True)
    return root


@pytest.fixture()
def _session(_chromium, _server, _audit):
    """An AuthBrowser driving a real Chromium page. `_open` launches Camoufox,
    which no test here has a binary for, so the context is built with the
    production kwargs and handed to the same object."""
    context = _chromium.new_context(**_new_context_kwargs(headed=False))
    session = AuthBrowser(agent_id="agent_test")
    session._context = context
    session._page = context.new_page()
    session._page.goto(f"{_server}/page.html")
    try:
        yield session
    finally:
        context.close()


def _step_to_completion(coro):
    """Run a coroutine whose every await completes without yielding.

    Playwright's sync API holds a running loop in this thread, so
    `asyncio.run` refuses outright; and with `_run_in_session` replaced by a
    direct call, nothing in the tool has anything to wait for. An await that
    does need a loop surfaces here rather than as a hang.
    """
    try:
        coro.send(None)
    except StopIteration as done:
        return done.value
    coro.close()
    raise AssertionError("browser_download awaited something needing a loop")


def _download(session, ctx, selector, **kwargs):
    """`browser_download` with the session wired in and the pinned executor
    out of the way: the Chromium page belongs to this thread."""
    import asyncio
    from unittest.mock import patch

    async def _direct(sess, verb, *args, **kw):
        kw.pop("_timeout", None)
        return getattr(sess, verb)(*args)

    with patch.object(browser_mod, "_get_session_or_error", lambda _id: session), \
         patch.object(browser_mod, "_get_session_lock", lambda _id: asyncio.Lock()), \
         patch.object(browser_mod, "_run_in_session", _direct):
        return _step_to_completion(
            browser_mod.browser_download(ctx, selector, **kwargs)
        )


def _tree(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def _saved_files(agent_root: Path) -> list[Path]:
    return [
        p for p in _tree(agent_root / "downloads")
        if p.name != DOWNLOAD_LEDGER_NAME
    ]


@pytest.mark.parametrize(
    "suggested,expected",
    [
        ("book.pdf", "book.pdf"),
        ("../../evil.exe", "evil.exe"),
        ("..\\..\\evil.exe", "evil.exe"),
        ("/etc/passwd", "passwd"),
        ("C:\\Windows\\System32\\evil.dll", "evil.dll"),
        ("\\\\server\\share\\report.bin", "report.bin"),
        ("CON.txt", "_CON.txt"),
        ("con", "_con"),
        ("nul.pdf", "_nul.pdf"),
        ("COM1", "_COM1"),
        ("LPT9.log", "_LPT9.log"),
        ("aux.tar.gz", "aux.tar.gz"),
        ("", "download"),
        (None, "download"),
        (".", "download"),
        ("..", "download"),
        ("...", "download"),
        ("   ", "download"),
        ("/", "download"),
        ("book.pdf...  ", "book.pdf"),
        ("book.pdf ", "book.pdf"),
        ("bo\x00ok\x1f\x7f.pdf", "book.pdf"),
        ("re:po|rt?.pdf", "re_po_rt_.pdf"),
        (".hidden.pdf", "hidden.pdf"),
    ],
)
def test_the_name_a_site_chose_is_reduced_to_one_it_cannot_abuse(
    suggested, expected,
):
    assert _safe_download_name(suggested) == expected


def test_a_very_long_name_keeps_its_extension_inside_the_cap():
    out = _safe_download_name(_LONG_STEM + ".pdf")
    assert out.endswith(".pdf")
    assert len(out) <= _DOWNLOAD_NAME_MAX
    assert out.startswith("aaa")


def test_no_device_name_survives_whatever_the_extension():
    for device in sorted(_WINDOWS_DEVICE_NAMES):
        for candidate in (device, f"{device}.txt", device.lower()):
            out = _safe_download_name(candidate)
            stem = out.rpartition(".")[0] or out
            assert stem.upper() not in _WINDOWS_DEVICE_NAMES, candidate


def test_a_sanitized_name_never_carries_a_separator_or_a_control_char():
    for hostile in ("../../x", "..\\..\\x", "a/b\\c", "\x00\x01", "C:x", "  ..  "):
        out = _safe_download_name(hostile)
        assert "/" not in out and "\\" not in out, hostile
        assert not re.search(r"[\x00-\x1f\x7f]", out), hostile
        assert out and out not in (".", ".."), hostile


def test_a_taken_name_is_not_overwritten_but_numbered(tmp_path):
    first = _unique_download_path(tmp_path, "book.pdf")
    first.write_bytes(b"one")
    second = _unique_download_path(tmp_path, "book.pdf")
    second.write_bytes(b"two")
    third = _unique_download_path(tmp_path, "book.pdf")
    assert first.name == "book.pdf"
    assert second.name == "book-1.pdf"
    assert third.name == "book-2.pdf"
    assert first.read_bytes() == b"one"


def test_a_name_without_an_extension_is_numbered_too(tmp_path):
    (tmp_path / "README").write_bytes(b"x")
    assert _unique_download_path(tmp_path, "README").name == "README-1"


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"%PDF-1.7\n...", "pdf"),
        (b"AT&TFORM\x00", "djvu"),
        (b"PK\x03\x04\x14\x00", "zip container"),
        (b"Rar!\x1a\x07\x00", "rar"),
        (b"7z\xbc\xaf\x27\x1c", "7z"),
        (b"\x1f\x8b\x08\x00", "gzip"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1", "ole"),
        (b"<!DOCTYPE html><html>", "html"),
        (b"<!doctype HTML>", "html"),
        (b"<html lang=en>", "html"),
        (b"\n\n  <!doctype html>", "html"),
        (b"\xef\xbb\xbf<!doctype html>", "html"),
        (b"<?xml version='1.0'?>", "markup"),
        (b"<svg xmlns=", "markup"),
        (b"just some prose", "unknown"),
        (b"", "unknown"),
    ],
)
def test_the_first_bytes_name_the_family(head, expected):
    assert _sniff_file_type(head) == expected


@pytest.mark.parametrize(
    "detected,name,contradicts",
    [
        ("pdf", "book.pdf", False),
        ("html", "book.pdf", True),
        ("markup", "book.pdf", True),
        ("zip container", "book.epub", False),
        ("zip container", "book.pdf", True),
        ("html", "page.html", False),
        ("unknown", "book.pdf", False),
        ("pdf", "bookwithnoextension", False),
    ],
)
def test_a_family_that_disagrees_with_the_extension_is_reported(
    detected, name, contradicts,
):
    assert _type_contradicts_extension(detected, name) is contradicts


def test_every_context_this_module_opens_accepts_a_download():
    """A bare context has the browser cancel the download before any tool can
    see a file."""
    assert _new_context_kwargs(headed=False)["accept_downloads"] is True
    assert _new_context_kwargs(headed=True)["accept_downloads"] is True
    assert _new_context_kwargs(headed=True)["no_viewport"] is True
    assert "no_viewport" not in _new_context_kwargs(headed=False)


def test_a_clicked_download_lands_in_the_sandbox_with_its_size_and_hash(
    _session, _agent_root, _audit,
):
    answer = _download(_session, _ctx(_agent_root), "#book")
    saved = _agent_root / "downloads" / "book.pdf"
    assert saved.is_file(), answer
    assert saved.read_bytes() == _BOOK
    assert "downloads/book.pdf" in answer
    assert f"{len(_BOOK):,} bytes" in answer
    assert hashlib.sha256(_BOOK).hexdigest() in answer
    assert "pdf" in answer
    assert 'suggested the name "book.pdf"' in answer
    rows = [r for r in _audit if r.get("action") == "download"]
    assert rows and rows[-1]["result"] == "ok"
    assert rows[-1]["sha256"] == hashlib.sha256(_BOOK).hexdigest()
    assert rows[-1]["byte_size"] == len(_BOOK)


def test_a_ref_from_the_snapshot_reaches_the_same_file(_session, _agent_root):
    """The click tool's own ref, not a second addressing scheme."""
    _tree_text, refs = _session.a11y_snapshot()
    ref = next(
        r for r, node in refs.items()
        if (node.get("name") or "").startswith("Download the book")
    )
    answer = _download(_session, _ctx(_agent_root), ref)
    assert (_agent_root / "downloads" / "book.pdf").is_file(), answer


@pytest.mark.parametrize("selector", ["#slash", "#backslash"])
def test_a_traversing_filename_cannot_write_outside_the_download_folder(
    _session, _agent_root, tmp_path, selector,
):
    answer = _download(_session, _ctx(_agent_root), selector)
    downloads = _agent_root / "downloads"
    inside = _tree(downloads)
    assert inside, answer
    outside = [p for p in _tree(tmp_path) if downloads not in p.parents]
    assert not outside, f"files written outside the download folder: {outside}"
    for path in inside:
        # Chromium mangles the separators itself, so the name can still read
        # `_.._evil.exe` — harmless as long as it stays one basename in one
        # folder, which is the invariant. `_safe_download_name` is what maps
        # the raw header to `evil.exe`, and it is asserted on directly above.
        assert "/" not in path.name and "\\" not in path.name
        assert path.resolve().parent == downloads.resolve()


def test_a_device_name_is_not_the_name_the_file_is_saved_under(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#device")
    saved = _saved_files(_agent_root)
    assert len(saved) == 1, answer
    stem = saved[0].name.rpartition(".")[0] or saved[0].name
    assert stem.upper() not in _WINDOWS_DEVICE_NAMES, saved[0].name


def test_a_four_hundred_character_name_is_capped_and_keeps_its_extension(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#long")
    saved = _saved_files(_agent_root)
    assert len(saved) == 1, answer
    assert len(saved[0].name) <= _DOWNLOAD_NAME_MAX
    assert saved[0].suffix == ".pdf"


def test_a_download_with_no_filename_still_gets_a_usable_one(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#nameless")
    saved = _saved_files(_agent_root)
    assert len(saved) == 1, answer
    assert saved[0].name
    assert "/" not in saved[0].name and "\\" not in saved[0].name
    assert saved[0].name.strip(". ") == saved[0].name


def test_the_same_file_twice_is_two_files_and_the_first_is_untouched(
    _session, _agent_root,
):
    _download(_session, _ctx(_agent_root), "#book")
    second = _download(_session, _ctx(_agent_root), "#book")
    downloads = _agent_root / "downloads"
    assert (downloads / "book.pdf").read_bytes() == _BOOK
    assert (downloads / "book-1.pdf").read_bytes() == _BOOK
    assert "book-1.pdf" in second


def test_a_file_over_the_cap_is_refused_and_leaves_nothing_behind(
    _session, _agent_root, monkeypatch,
):
    monkeypatch.setattr(browser_mod, "DOWNLOAD_MAX_BYTES", 100)
    answer = _download(_session, _ctx(_agent_root), "#big")
    assert "Refused" in answer and "4,096 bytes" in answer
    assert "100-byte cap" in answer
    assert _tree(_agent_root / "downloads") == []


@pytest.mark.parametrize("directory", ["../outside", "..", "a/../../b"])
def test_a_directory_outside_the_sandbox_is_refused_before_any_click(
    _session, _agent_root, tmp_path, directory,
):
    answer = _download(_session, _ctx(_agent_root), "#book", directory=directory)
    assert "refused" in answer.lower(), answer
    assert _tree(tmp_path) == [], _tree(tmp_path)


def test_an_absolute_directory_outside_the_grants_is_refused(
    _session, _agent_root, tmp_path,
):
    """Extended write is on for this agent and this path is still not its:
    what refuses is the grant list, not the shape of the path."""
    outside = tmp_path / "elsewhere"
    answer = _download(
        _session, _ctx(_agent_root), "#book", directory=str(outside),
    )
    assert "refused" in answer.lower(), answer
    assert not outside.exists()
    assert _tree(tmp_path) == []


def test_an_absolute_directory_inside_a_granted_path_is_where_the_file_lands(
    _session, _agent_root, tmp_path,
):
    """The twin the schema now describes. Observed live on 2026-09-21: a
    library folder granted in Agent Permissions took the file, and the
    sentence that called every absolute path refused was the thing wrong."""
    library = tmp_path / "library"
    library.mkdir()
    target = library / "raw" / "command-and-decision"
    answer = _download(
        _session, _ctx(_agent_root, granted=[library]), "#book",
        directory=str(target),
    )
    saved = target / "book.pdf"
    assert saved.read_bytes() == _BOOK, answer
    assert str(saved) in answer
    assert hashlib.sha256(_BOOK).hexdigest() in answer
    records = [
        json.loads(line)
        for line in (target / DOWNLOAD_LEDGER_NAME)
        .read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(records) == 1
    assert records[0]["sha256"] == hashlib.sha256(_BOOK).hexdigest()
    # Nothing was written into the sandbox on the way.
    assert _tree(_agent_root) == []


def test_an_ordinary_link_says_no_download_started_and_names_the_page(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#plain", timeout_seconds=3)
    assert "No download started within 3s" in answer
    assert "other.html" in answer
    downloads = _agent_root / "downloads"
    assert not downloads.exists() or _tree(downloads) == []


def test_a_click_that_started_nothing_names_both_urls_and_the_tab_count(
    _session, _agent_root,
):
    """The facts the owner asked for after the first live use, and no guess.

    The old answer said the element "may be an ordinary link" — on the live
    run the button was the right one and the network was down, so the guess
    sent the agent looking in the wrong place.
    """
    answer = _download(_session, _ctx(_agent_root), "#plain", timeout_seconds=3)
    assert "No download started within 3s" in answer
    # Before the click and now, and whether that is a change.
    assert "page.html" in answer
    assert "other.html" in answer
    assert "changed" in answer
    # A file opened in a new tab is the other reading of a silent click.
    assert "1 tab" in answer
    # What the timeout does and does not bound.
    assert "start" in answer.lower()
    assert "transfer" in answer
    # What the page offers, and here it offers nothing off its own host.
    assert "This page has no links to other hosts." in answer
    assert "ordinary link" not in answer
    assert "may be" not in answer


def _links_line(answer: str) -> str:
    return next(
        line for line in answer.splitlines()
        if line.startswith("Links on this page to other hosts:")
    )


def test_a_page_that_started_nothing_names_the_links_it_carries_to_other_hosts(
    _session, _agent_root, _server, _audit,
):
    """The fallback link a site puts beside a button that does nothing.

    Observed live on 2026-09-21: the page said "if the download did not start,
    use this link" and pointed at the file host, while the answer looked only
    at the address bar and the agent went looking elsewhere.
    """
    _session._page.goto(f"{_server}/fallback.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    line = _links_line(answer)
    assert _FALLBACK_TEXT in line
    assert f"{_FILE_HOST}/book.pdf" in line
    # Its own host is not a lead, and neither is a scheme that fetches nothing.
    assert "other.html" not in line
    assert "javascript:" not in answer
    assert "mailto:" not in answer
    # `javascript:` and `mailto:` have no host and fall to the host check;
    # an ftp link HAS one, so only the scheme filter keeps it out.
    assert "ftp:" not in answer
    # The guess that was removed stays removed.
    assert "ordinary link" not in answer
    assert "may be" not in answer
    rows = [r for r in _audit if r.get("action") == "download"]
    assert rows[-1]["cross_host_links"] == 1
    assert _FILE_HOST not in json.dumps(rows[-1])


def test_a_page_whose_links_are_all_its_own_says_there_are_none(
    _session, _agent_root, _server,
):
    _session._page.goto(f"{_server}/samehost.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    assert "This page has no links to other hosts." in answer


def test_a_page_of_mirrors_names_five_of_them_and_each_href_once(
    _session, _agent_root, _server, _audit,
):
    _session._page.goto(f"{_server}/manyhosts.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    line = _links_line(answer)
    assert line.count(" -> ") == browser_mod._NO_DOWNLOAD_LINKS_MAX == 5
    # The repeated href did not take a slot from the fifth host.
    assert "mirror5.example.com" in line
    assert "mirror6.example.com" not in answer
    rows = [r for r in _audit if r.get("action") == "download"]
    assert rows[-1]["cross_host_links"] == 5


def test_the_page_side_cap_holds_without_the_python_one(_session, _server):
    """Two caps, and either hides the other's absence from a test that goes
    through both: this is the page's own."""
    _session._page.goto(f"{_server}/manyhosts.html")
    raw = _session._page.evaluate(browser_mod._CROSS_HOST_LINKS_JS, 5)
    assert len(raw) == 5, raw


def test_the_python_side_cap_holds_when_the_page_hands_over_more(
    _session, _agent_root, _server, monkeypatch,
):
    nine = ", ".join(
        f"{{href: 'http://m{i}.example.com/f', text: 'm{i}'}}" for i in range(9)
    )
    monkeypatch.setattr(
        browser_mod, "_CROSS_HOST_LINKS_JS", f"(max) => [{nine}]",
    )
    _session._page.goto(f"{_server}/fallback.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    assert _links_line(answer).count(" -> ") == 5, answer


def test_a_link_text_longer_than_its_bound_arrives_cut(
    _session, _agent_root, _server,
):
    _session._page.goto(f"{_server}/longtext.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    line = _links_line(answer)
    shown = line.split('"')[1]
    assert len(shown) <= browser_mod._NO_DOWNLOAD_LINK_TEXT_LIMIT, shown
    assert shown.endswith("…"), shown


def test_a_href_longer_than_the_bound_arrives_cut_not_whole(
    _session, _agent_root, _server,
):
    _session._page.goto(f"{_server}/longquery.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    line = _links_line(answer)
    assert "…" in line
    assert _LONG_QUERY not in line
    assert len(line) <= 200 + len("Links on this page to other hosts: ") + 40


def test_a_page_that_cannot_be_asked_answers_with_the_four_facts_and_no_guess(
    _session, _agent_root, _server, monkeypatch,
):
    """A page gone or navigating is a page with no answer, not an exception —
    and not a claim that it carries no links either."""
    monkeypatch.setattr(
        browser_mod, "_CROSS_HOST_LINKS_JS", "(max) => { throw new Error('x'); }",
    )
    _session._page.goto(f"{_server}/fallback.html")
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    lines = answer.splitlines()
    assert len(lines) == 5, answer
    assert "No download started within 3s" in lines[0]
    assert "did not change" in lines[1]
    assert "1 tab" in lines[2]
    # The advice the fourth line carries is about the click, not the page's
    # links, and is the same whatever the page could not answer.
    assert "browser_snapshot" in lines[3]
    assert "transfer" in lines[4]
    assert "links" not in answer.lower()


def test_a_click_that_changed_nothing_says_the_url_did_not_change(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#inert", timeout_seconds=3)
    assert "No download started within 3s" in answer
    assert "did not change" in answer
    assert "1 tab" in answer


def test_the_start_timeout_defaults_to_the_owners_three_minutes_everywhere():
    """One source for the number: the schema, the handler and the session
    method all read the same constant."""
    assert browser_mod._DOWNLOAD_TIMEOUT_DEFAULT == 180
    handler_default = inspect.signature(
        browser_mod.browser_download
    ).parameters["timeout_seconds"].default
    assert handler_default == browser_mod._DOWNLOAD_TIMEOUT_DEFAULT
    session_default = inspect.signature(
        AuthBrowser.download
    ).parameters["timeout"].default
    assert session_default == browser_mod._DOWNLOAD_TIMEOUT_DEFAULT * 1000
    entry = next(
        t for t in browser_mod.get_tools() if t.name == "browser_download"
    )
    schema = entry.schema["parameters"]["properties"]["timeout_seconds"]
    assert schema["default"] == browser_mod._DOWNLOAD_TIMEOUT_DEFAULT
    assert "180" in schema["description"]
    assert "start" in schema["description"].lower()
    assert "transfer" in schema["description"]


def test_the_transfer_allowance_is_the_cap_at_one_named_rate():
    rate = browser_mod._DOWNLOAD_RATE_BYTES_PER_SEC
    assert browser_mod._DOWNLOAD_SAVE_TIMEOUT_SEC == math.ceil(
        browser_mod.DOWNLOAD_MAX_BYTES / rate
    )
    # 512 MiB at the rate measured live on 2026-09-21 is seven-odd minutes.
    assert 7 * 60 <= browser_mod._DOWNLOAD_SAVE_TIMEOUT_SEC <= 9 * 60


def test_the_tool_ceiling_outlasts_the_longest_start_plus_that_transfer():
    """The ToolEntry ceiling must not fire first: the session's own timeout
    answers with a sentence, TOOL_TIMEOUT answers with a number."""
    entry = next(
        t for t in browser_mod.get_tools() if t.name == "browser_download"
    )
    longest_session_wait = (
        browser_mod._DOWNLOAD_TIMEOUT_MAX + browser_mod._DOWNLOAD_SAVE_TIMEOUT_SEC
    )
    assert entry.timeout_sec > longest_session_wait
    assert browser_mod._DOWNLOAD_TIMEOUT_MAX >= browser_mod._DOWNLOAD_TIMEOUT_DEFAULT


def test_the_session_call_gets_the_start_wait_plus_the_transfer_allowance(
    _session, _agent_root,
):
    """The wrapper's own budget, so a slow transfer is not cut by the wait
    for the start."""
    import asyncio
    from unittest.mock import patch

    seen: dict = {}

    async def _spy(sess, verb, *args, **kwargs):
        seen["args"] = args
        seen["timeout"] = kwargs.get("_timeout")
        return {
            "status": "no_download", "page_url": "about:blank",
            "url_before": "about:blank", "tab_count": 1,
            "timeout_ms": args[2], "error": "TimeoutError",
        }

    with patch.object(
        browser_mod, "_get_session_or_error", lambda _id: _session,
    ), patch.object(
        browser_mod, "_get_session_lock", lambda _id: asyncio.Lock(),
    ), patch.object(browser_mod, "_run_in_session", _spy):
        _step_to_completion(
            browser_mod.browser_download(
                _ctx(_agent_root), "#book", timeout_seconds=1000,
            )
        )
    # Clamped to the upper bound, and the transfer allowance on top of it.
    assert seen["args"][2] == browser_mod._DOWNLOAD_TIMEOUT_MAX * 1000
    assert seen["timeout"] == (
        browser_mod._DOWNLOAD_TIMEOUT_MAX + browser_mod._DOWNLOAD_SAVE_TIMEOUT_SEC
    )


def test_a_login_page_under_a_pdf_name_is_named_as_one_in_the_first_line(
    _session, _agent_root,
):
    answer = _download(_session, _ctx(_agent_root), "#wall")
    first = answer.split("\n")[0]
    assert "HTML page, not a file" in first, answer
    assert ".pdf" in first
    assert (_agent_root / "downloads" / "wall.pdf").read_bytes() == _WALL


def test_a_stale_ref_is_refused_rather_than_waited_out(_session, _agent_root):
    answer = _download(_session, _ctx(_agent_root), "@e9999")
    assert "unknown ref" in answer
    assert "a11y_snapshot" in answer


def _ledger(agent_root: Path) -> list[dict]:
    path = agent_root / "downloads" / DOWNLOAD_LEDGER_NAME
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_every_saved_file_gets_one_ledger_line_with_its_hash_and_origin(
    _session, _agent_root,
):
    _download(_session, _ctx(_agent_root), "#book", note="Volume I, cat 42")
    _download(_session, _ctx(_agent_root), "#book")
    records = _ledger(_agent_root)
    assert len(records) == 2
    for rec in records:
        for field in (
            "saved_at", "saved_path", "bytes", "sha256", "detected_type",
            "suggested_filename", "url", "page_url", "page_title", "note",
        ):
            assert field in rec, field
        assert rec["sha256"] == hashlib.sha256(_BOOK).hexdigest()
        assert rec["bytes"] == len(_BOOK)
        assert rec["detected_type"] == "pdf"
        assert rec["suggested_filename"] == "book.pdf"
        assert rec["url"].endswith("/book.pdf")
        assert rec["page_url"].endswith("/page.html")
        assert rec["page_title"] == "The download page"
    assert records[0]["note"] == "Volume I, cat 42"
    assert records[1]["note"] == ""
    assert records[0]["saved_path"] == "downloads/book.pdf"
    assert records[1]["saved_path"] == "downloads/book-1.pdf"


def test_the_ledger_records_what_the_bytes_were_not_what_the_name_claimed(
    _session, _agent_root,
):
    _download(_session, _ctx(_agent_root), "#wall")
    rec = _ledger(_agent_root)[-1]
    assert rec["detected_type"] == "html"
    assert rec["saved_path"] == "downloads/wall.pdf"


def test_the_answer_ends_with_what_the_folder_now_holds(_session, _agent_root):
    first = _download(_session, _ctx(_agent_root), "#book")
    assert "holds 1 record(s), 1 of them saved today" in first
    second = _download(_session, _ctx(_agent_root), "#book")
    assert "holds 2 record(s), 2 of them saved today" in second


def test_a_refused_or_failed_download_records_nothing(
    _session, _agent_root, monkeypatch,
):
    monkeypatch.setattr(browser_mod, "DOWNLOAD_MAX_BYTES", 100)
    _download(_session, _ctx(_agent_root), "#big")
    _download(_session, _ctx(_agent_root), "#plain", timeout_seconds=3)
    assert not (_agent_root / "downloads" / DOWNLOAD_LEDGER_NAME).exists()


def test_a_ledger_that_cannot_be_written_does_not_cost_the_file(
    _session, _agent_root, monkeypatch,
):
    monkeypatch.setattr(
        browser_mod, "_append_download_record",
        lambda directory, record: "OSError: disk is full",
    )
    answer = _download(_session, _ctx(_agent_root), "#book")
    assert (_agent_root / "downloads" / "book.pdf").read_bytes() == _BOOK
    assert "disk is full" in answer
    assert "The file is saved" in answer


def test_the_tool_is_registered_and_off_until_someone_turns_it_on():
    entry = next(
        t for t in browser_mod.get_tools() if t.name == "browser_download"
    )
    assert entry.default_enabled is False
    assert entry.handler is browser_mod.browser_download
    desc = entry.schema["description"]
    assert "sandbox" in desc
    assert "charge per download" in desc
    assert "browser_snapshot" in desc
    assert set(entry.schema["parameters"]["properties"]) == {
        "ref_or_selector", "directory", "timeout_seconds", "note",
    }


def test_the_directory_description_says_what_the_resolver_actually_does():
    """`_resolve_file_path` sends an absolute path to the extended-path write
    gate; the schema used to call every one of them refused."""
    entry = next(
        t for t in browser_mod.get_tools() if t.name == "browser_download"
    )
    desc = entry.schema["parameters"]["properties"]["directory"]["description"]
    assert "Extended Paths" in desc
    assert _DOWNLOAD_DIR_DEFAULT in desc
    assert "An absolute path, '..' or a drive letter is refused" not in desc
