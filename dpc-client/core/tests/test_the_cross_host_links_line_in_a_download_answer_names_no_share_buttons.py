"""The links line names the link worth having, not the share row above it.

A share button is a link to another host that carries this page's own address
inside it, and it usually has no text — an icon and an aria-label. Five of
them ahead of the one real link filled the whole line and said nothing.

Chromium against a local `http.server`, the way the download tests do: the
filter and the ordering live in page JS, and only a real engine runs it.
Camoufox/Firefox, the production engine, is NOT exercised here.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dpc_client_core.dpc_agent.tools import browser as browser_mod
from dpc_client_core.dpc_agent.tools.browser import (
    AuthBrowser,
    _new_context_kwargs,
)

_FILE_HOST = "http://files.example.com"
_REAL_TEXT = "If the download did not start, use this link"

# The five a real page put above its one download link, each carrying the
# page's own address as the thing to share.
_SHARE_HOSTS = (
    "https://www.facebook.com/sharer.php?u=",
    "https://vk.com/share.php?url=",
    "https://connect.mail.ru/share?url=",
    "https://www.linkedin.com/shareArticle?url=",
    "https://twitter.com/intent/tweet?url=",
)

_ANCHOR_DECORATION = (
    '<img src="/icon.png" alt="">',
    "",
    "<span></span>",
    "",
    "",
)


def _share_page(page_url: str) -> str:
    from urllib.parse import quote

    rows = "".join(
        f'  <a href="{host}{quote(page_url, safe="")}">{deco}</a>\n'
        for host, deco in zip(_SHARE_HOSTS, _ANCHOR_DECORATION)
    )
    return (
        "<!doctype html>\n<html><head><title>Share row first</title></head><body>\n"
        '  <button id="inert" type="button">A button that starts nothing</button>\n'
        f"{rows}"
        f'  <a href="{_FILE_HOST}/book.pdf">{_REAL_TEXT}</a>\n'
        "</body></html>\n"
    )


_NAMELESS_PAGE = f"""<!doctype html>
<html><head><title>Links with no text of their own</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="{_FILE_HOST}/one" aria-label="From the label"></a>
  <a href="{_FILE_HOST}/two" title="From the title"></a>
  <a href="{_FILE_HOST}/three"><img src="/icon.png" alt="From the image"></a>
  <a href="{_FILE_HOST}/four"></a>
</body></html>
"""

_MIXED_PAGE = f"""<!doctype html>
<html><head><title>Named and nameless</title></head><body>
  <button id="inert" type="button">A button that starts nothing</button>
  <a href="{_FILE_HOST}/blank-one"></a>
  <a href="{_FILE_HOST}/named-one">First named</a>
  <a href="{_FILE_HOST}/blank-two"></a>
  <a href="{_FILE_HOST}/named-two">Second named</a>
</body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_base = ""

    def do_GET(self):  # noqa: N802 - http.server's own spelling
        if self.path == "/nameless.html":
            body = _NAMELESS_PAGE
        elif self.path == "/mixed.html":
            body = _MIXED_PAGE
        else:
            body = _share_page(f"{self.server_base}{self.path}")
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def _server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.server_base = f"http://127.0.0.1:{httpd.server_port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Handler.server_base
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


@pytest.fixture()
def _session(_chromium, _server, _audit):
    context = _chromium.new_context(**_new_context_kwargs(headed=False))
    session = AuthBrowser(agent_id="agent_test")
    session._context = context
    session._page = context.new_page()
    try:
        yield session
    finally:
        context.close()


def _links(session, url: str) -> list[dict]:
    session._page.goto(url)
    return session._cross_host_links()


def test_five_share_buttons_are_gone_and_the_real_link_is_the_one_named(
    _session, _server,
):
    links = _links(_session, f"{_server}/share.html")
    assert [link["href"] for link in links] == [f"{_FILE_HOST}/book.pdf"]
    assert links[0]["text"] == _REAL_TEXT
    for host in ("facebook", "vk.com", "mail.ru", "linkedin", "twitter"):
        assert all(host not in link["href"] for link in links), links


def test_the_page_side_drops_the_share_row_without_the_python_side(
    _session, _server,
):
    """Two filters, and either hides the other's absence from a test that
    goes through both: this is the page's own."""
    _session._page.goto(f"{_server}/share.html")
    raw = _session._page.evaluate(browser_mod._CROSS_HOST_LINKS_JS, 5)
    assert [item["href"] for item in raw] == [f"{_FILE_HOST}/book.pdf"]


def test_a_link_with_no_text_borrows_the_label_the_title_then_the_image_alt(
    _session, _server,
):
    links = _links(_session, f"{_server}/nameless.html")
    by_href = {link["href"]: link["text"] for link in links}
    assert by_href[f"{_FILE_HOST}/one"] == "From the label"
    assert by_href[f"{_FILE_HOST}/two"] == "From the title"
    assert by_href[f"{_FILE_HOST}/three"] == "From the image"
    assert by_href[f"{_FILE_HOST}/four"] == ""


def test_a_named_link_comes_before_a_nameless_one_and_the_rest_keeps_order(
    _session, _server,
):
    links = _links(_session, f"{_server}/mixed.html")
    assert [link["href"] for link in links] == [
        f"{_FILE_HOST}/named-one",
        f"{_FILE_HOST}/named-two",
        f"{_FILE_HOST}/blank-one",
        f"{_FILE_HOST}/blank-two",
    ]


def test_the_python_side_orders_and_caps_what_the_page_hands_over(
    _session, _server, monkeypatch,
):
    """The cap is applied after the ordering on both sides: a page that
    hands over nine links must not spend all five slots on the nameless
    ones it happened to list first."""
    blanks = ", ".join(
        f"{{href: 'http://m{i}.example.com/f', text: ''}}" for i in range(6)
    )
    named = ", ".join(
        f"{{href: 'http://n{i}.example.com/f', text: 'n{i}'}}" for i in range(3)
    )
    monkeypatch.setattr(
        browser_mod, "_CROSS_HOST_LINKS_JS", f"(max) => [{blanks}, {named}]",
    )
    links = _links(_session, f"{_server}/mixed.html")
    assert len(links) == browser_mod._NO_DOWNLOAD_LINKS_MAX == 5
    assert [link["text"] for link in links[:3]] == ["n0", "n1", "n2"]
    assert [link["text"] for link in links[3:]] == ["", ""]
