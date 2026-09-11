"""The window's cookies are written back, and no reading of the page can stop
that.

A page test used to stand in front of the write: the snapshot was stored only
if the HTML showed no sign-in step and yielded readable content. Measured
against a live, fully signed-in `x.com/home` — 427 655 characters of HTML, 455
of them text — the marker that matched was `challenge-platform`, inside
Cloudflare's own `<script src="/cdn-cgi/challenge-platform/...">`, which the
site serves on every page it has. The predicate therefore answered "this page
is asking for a login" on every page of that site, and the person's sign-in
could not be saved at all.

The write is unconditional again. What replaces the guess is two facts, and a
page can defeat neither:

* a scoped window opens carrying the vault's own jar for its scope, so its
  snapshot already holds the login it might otherwise displace — pinned in
  `test_a_visible_window_is_ungated_and_a_headless_one_is_not.py`;
* `save_cookies` refuses a snapshot with nothing sendable in it over a jar
  that has something, which is the case of a site clearing its own cookies.

Behind both, `restore_previous_cookies` still keeps one generation, and it is
reachable from the UI now rather than from tests only.

Nothing here touches a network or a browser: the context and the page are
stubs and every path is driven directly rather than through `browse_page`, so
a guard removed by a mutation cannot turn a test into a live fetch.
"""

import asyncio
import json
import time

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


AGENT = "agent_a"

# The page that was measured, in miniature: a signed-in timeline whose HTML
# carries the Cloudflare script tag every page of that site carries.
CLOUDFLARE_SCRIPT = (
    '<script async src="/cdn-cgi/challenge-platform/scripts/jsd/api.js'
    '?onload=jsdOnload"></script>'
)
SIGNED_IN_HTML = (
    "<html><head>" + CLOUDFLARE_SCRIPT + "</head><body><h1>Your timeline</h1>"
    "<p>Ordinary page content, long enough that an extractor returns "
    "something rather than nothing.</p></body></html>"
)
LOGIN_HTML = (
    '<html><body><form><input name="username"><input type="password" '
    'name="password"></form></body></html>'
)
BLANK_HTML = "<html><body><div></div></body></html>"


# ── doubles ──────────────────────────────────────────────────────────────


class _Page:
    """A page that answers `content()`, or refuses to."""

    def __init__(self, html=SIGNED_IN_HTML, raises=None):
        self._html = html
        self._raises = raises
        self.url = f"https://{TEST_DOMAIN}/home"

    def content(self):
        if self._raises is not None:
            raise self._raises
        return self._html

    def is_closed(self):
        return False


class _Context:
    def __init__(self, cookies_payload):
        self.cookies_payload = list(cookies_payload)
        self.added: list[list[dict]] = []
        self.cookies_calls = 0

    def add_cookies(self, cookies):
        self.added.append(list(cookies))

    def route(self, pattern, handler):
        pass

    def new_page(self):
        return _Page()

    def cookies(self):
        self.cookies_calls += 1
        return list(self.cookies_payload)

    def close(self):
        pass


def _guest_cookies(domain=TEST_DOMAIN):
    """What a site hands a window nobody has signed into: an anonymous id
    and an anti-bot cookie."""
    return [
        {
            "name": "guest_id", "value": "v1%3A17", "domain": f".{domain}",
            "path": "/", "expires": time.time() + 86400,
            "secure": True, "httpOnly": False, "sameSite": "Lax",
        },
        {
            "name": "cf_clearance", "value": "x" * 426, "domain": f".{domain}",
            "path": "/", "expires": time.time() + 86400,
            "secure": True, "httpOnly": True, "sameSite": "None",
        },
    ]


def _session_cookies(domain=TEST_DOMAIN, value="real-session-token", expires=None):
    return [
        {
            "name": "auth_token", "value": value, "domain": f".{domain}",
            "path": "/",
            "expires": time.time() + 86400 if expires is None else expires,
            "secure": True, "httpOnly": True, "sameSite": "Lax",
        },
    ]


def _to_playwright(cookies):
    return [
        {**c, "httpOnly": c.pop("httpOnly", False)} for c in (dict(x) for x in cookies)
    ]


def _vault_bytes(home) -> bytes:
    path = home / "agents" / AGENT / "web_credentials.enc"
    return path.read_bytes() if path.exists() else b""


def _audit_rows(home):
    path = home / "agents" / AGENT / "web_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed_stored_login(value="real-session-token"):
    from dpc_client_core import web_auth

    web_auth.save_cookies(AGENT, TEST_DOMAIN, _session_cookies(value=value))


def _browser(html=SIGNED_IN_HTML, raises=None, cookies=None, domains=None):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id=AGENT, domains=domains or [TEST_DOMAIN], headed=True,
    )
    ab._context = _Context(_to_playwright(
        _session_cookies() if cookies is None else cookies
    ))
    ab._page = _Page(html=html, raises=raises)
    return ab


def _direct_session_runner(monkeypatch, browser_mod, ab):
    """Run `browser_close` against the stub without an executor thread."""

    async def _direct(session, method_name, *args, **kwargs):
        kwargs.pop("_touch", None)
        kwargs.pop("_timeout", None)
        return getattr(session, method_name)(*args, **kwargs)

    monkeypatch.setattr(browser_mod, "_run_in_session", _direct)
    monkeypatch.setitem(browser_mod._active_browser_sessions, AGENT, ab)


# ── the page decides nothing ─────────────────────────────────────────────


def test_a_signed_in_page_that_carries_a_cloudflare_script_is_written_back(
    vault_home,
):
    """The measured failure, in one assertion. This page is signed in and its
    HTML carries the script URL the removed predicate matched on, four times
    in a row, on a person's own logged-in profile."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-old-one")

    ab = _browser(html=SIGNED_IN_HTML, cookies=_session_cookies(value="fresh"))
    assert ab._persist_session_cookies() == "written"

    saved = web_auth.load_cookies(AGENT, TEST_DOMAIN)
    assert [c["name"] for c in saved] == ["auth_token"]
    assert saved[0]["value"] == "fresh"


def test_the_removed_markers_are_no_longer_consulted_for_anything(vault_home):
    """The predicate is gone from the write path, not merely tuned: a page
    whose only content is the Cloudflare script tag is written back too."""
    from dpc_client_core import web_auth

    ab = _browser(
        html="<html><head>" + CLOUDFLARE_SCRIPT + "</head><body></body></html>",
        cookies=_session_cookies(value="written-anyway"),
    )
    assert ab._persist_session_cookies() == "written"
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN)[0]["value"] == "written-anyway"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"html": LOGIN_HTML},
        {"html": BLANK_HTML},
        {"raises": RuntimeError("Target page, context or browser has been closed")},
    ],
    ids=["a_login_form", "a_blank_render", "a_page_that_cannot_be_read"],
)
def test_no_state_of_the_page_can_stop_the_write(vault_home, kwargs):
    """Whatever the window is showing, what it holds is what gets stored. The
    person is looking at the window; the HTML is not the record of what they
    did in it."""
    from dpc_client_core import web_auth

    ab = _browser(cookies=_session_cookies(value="stored"), **kwargs)
    assert ab._persist_session_cookies() == "written"
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN)[0]["value"] == "stored"


def test_a_session_with_no_page_at_all_still_writes(vault_home):
    from dpc_client_core import web_auth

    ab = _browser(cookies=_session_cookies(value="stored"))
    ab._page = None
    assert ab._persist_session_cookies() == "written"
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN)[0]["value"] == "stored"


def test_the_close_writes_what_the_window_ended_with(vault_home):
    """Close is where a finished sign-in most often lands — the person signs
    in, shuts the window, and says so in the chat."""
    from dpc_client_core import web_auth

    ab = _browser(cookies=_session_cookies(value="signed-in-by-hand"))
    ab.close()

    assert web_auth.load_cookies(AGENT, TEST_DOMAIN)[0]["value"] == (
        "signed-in-by-hand"
    )


# ── the guard that is about cookies, not about a page ────────────────────


def test_an_empty_snapshot_does_not_replace_a_stored_jar(vault_home):
    """A site that clears its own cookies leaves the window holding nothing.
    Nothing must not become the stored login.

    Byte-for-byte equality of the vault file is the measurement: a Fernet
    blob is re-randomised on every write, so an unchanged file proves no
    write was attempted at all."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    before = _vault_bytes(vault_home)
    assert before

    assert web_auth.save_cookies(AGENT, TEST_DOMAIN, []) is False

    assert _vault_bytes(vault_home) == before
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN)[0]["value"] == "the-real-one"


def test_a_snapshot_of_nothing_but_elapsed_cookies_is_an_empty_snapshot(
    vault_home,
):
    """An expired jar sends nothing, so it replaces nothing. Counting the
    cookies rather than asking whether any can be sent would let this one
    through on a count of two."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    before = _vault_bytes(vault_home)

    stale = _session_cookies(value="dead", expires=time.time() - 3600)
    assert web_auth.save_cookies(AGENT, TEST_DOMAIN, stale) is False

    assert _vault_bytes(vault_home) == before


def test_an_empty_snapshot_is_stored_when_there_is_nothing_to_lose(vault_home):
    """The refusal is about what would be destroyed, not about what arrives —
    a jar that holds nothing is not protected from an empty snapshot, or the
    first write for a site could never happen."""
    from dpc_client_core import web_auth

    assert web_auth.save_cookies(AGENT, TEST_DOMAIN, []) is True
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN) == []
    assert web_auth.save_cookies(AGENT, TEST_DOMAIN, _session_cookies()) is True


def test_the_guard_cannot_be_reached_from_the_page(vault_home):
    """The condition reads the cookies in hand. A page showing a signed-in
    session does not talk it into replacing a login with nothing."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    before = _vault_bytes(vault_home)

    ab = _browser(
        html=SIGNED_IN_HTML,
        cookies=_session_cookies(value="dead", expires=time.time() - 3600),
    )
    reason = ab._persist_session_cookies()

    assert reason == f"nothing_sendable_in_snapshot:{TEST_DOMAIN}"
    assert _vault_bytes(vault_home) == before


def test_a_refused_write_is_recorded_and_said_out_loud(vault_home, monkeypatch):
    """A refusal the person is not told about is indistinguishable from a
    silent overwrite."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    _seed_stored_login()
    ab = _browser(cookies=_session_cookies(value="dead", expires=time.time() - 3600))
    ab._persist_session_cookies()

    declined = [
        r for r in _audit_rows(vault_home)
        if r.get("action") == "cookie_writeback"
    ]
    assert len(declined) == 1
    assert declined[0]["result"] == "declined"
    assert declined[0]["reason"].startswith("nothing_sendable_in_snapshot:")

    _direct_session_runner(monkeypatch, browser_mod, ab)

    class _Ctx:
        agent_root = type("R", (), {"name": AGENT})()

    answer = asyncio.run(browser_mod.browser_close(_Ctx()))
    assert "NOTHING WAS SAVED" in answer
    assert "nothing was lost" in answer
    assert "STOP and wait" in answer


def test_a_window_carrying_nothing_for_its_own_site_says_so(vault_home):
    """Not a refusal — there was nothing in scope to write. The jar is
    untouched either way, and the person hears which it was."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    before = _vault_bytes(vault_home)

    ab = _browser(cookies=_session_cookies(domain="example.net", value="other"))
    assert ab._persist_session_cookies() == "no_cookies_in_scope"

    assert _vault_bytes(vault_home) == before
    assert ab._last_writeback_decline == "no_cookies_in_scope"
    assert web_auth.load_cookies(AGENT, "example.net") is None


def test_a_written_snapshot_announces_nothing(vault_home, monkeypatch):
    """The counterpart, so the notice cannot be a constant: a close that did
    write says only that it closed."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ab = _browser(cookies=_session_cookies())
    _direct_session_runner(monkeypatch, browser_mod, ab)

    class _Ctx:
        agent_root = type("R", (), {"name": AGENT})()

    assert asyncio.run(browser_mod.browser_close(_Ctx())) == "Browser session closed"


# ── the insurance behind the guard ───────────────────────────────────────


def test_a_replaced_jar_is_still_reachable(vault_home):
    """The write is unconditional, so the copy kept behind it is what holds
    when a window stores something nobody wanted stored."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    web_auth.save_cookies(AGENT, TEST_DOMAIN, _guest_cookies())

    assert [c["name"] for c in web_auth.load_cookies(AGENT, TEST_DOMAIN)] == [
        "guest_id", "cf_clearance",
    ]
    assert web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN) is True

    restored = web_auth.load_cookies(AGENT, TEST_DOMAIN)
    assert [c["name"] for c in restored] == ["auth_token"]
    assert restored[0]["value"] == "the-real-one"


def test_a_restore_is_itself_undoable(vault_home):
    """A restore aimed at the wrong generation must not be the new loss."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    web_auth.save_cookies(AGENT, TEST_DOMAIN, _guest_cookies())
    web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN)

    assert web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN) is True
    assert [c["name"] for c in web_auth.load_cookies(AGENT, TEST_DOMAIN)] == [
        "guest_id", "cf_clearance",
    ]


def test_there_is_nothing_to_restore_before_anything_was_displaced(vault_home):
    from dpc_client_core import web_auth

    assert web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN) is False
    _seed_stored_login()
    assert web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN) is False


def test_the_kept_copy_is_one_generation_deep(vault_home):
    """A history would let the same mistake, repeated, bury the good jar out
    of reach under copies of itself."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-real-one")
    for _ in range(3):
        web_auth.save_cookies(AGENT, TEST_DOMAIN, _guest_cookies())

    vault = web_auth._load_vault(AGENT)
    entry = vault["domains"][TEST_DOMAIN]
    assert "previous" in entry
    assert "previous" not in entry["previous"]


def test_the_listing_says_whether_there_is_anything_to_put_back(vault_home):
    """The control that offers a restore has to know whether one exists, or
    it offers a no-op and reads as broken."""
    from dpc_client_core import web_auth

    _seed_stored_login()
    assert web_auth.list_domains(AGENT)[0]["has_previous"] is False

    web_auth.save_cookies(AGENT, TEST_DOMAIN, _guest_cookies())
    assert web_auth.list_domains(AGENT)[0]["has_previous"] is True


# ── what the markers still decide, and what they may match ───────────────


def test_no_marker_matches_the_url_of_a_script(vault_home):
    """The rule the list is built on: a marker names text a person can read
    or an attribute a form must carry. A token that can sit in a path, a
    script URL or a JSON key describes the site, not the page, and answers
    the same on every page of it."""
    from dpc_client_core.dpc_agent.tools.browser import _LOGIN_PAGE_MARKERS

    for marker in _LOGIN_PAGE_MARKERS:
        is_attribute = marker.startswith(("type=", "name=", "autocomplete="))
        is_visible_sentence = " " in marker
        assert is_attribute or is_visible_sentence, marker

    for removed in (
        "challenge-platform", "cf-challenge", "cf_chl", "challenge-form",
        "two_factor", "two-factor",
    ):
        assert removed not in _LOGIN_PAGE_MARKERS


def test_a_cloudflare_script_tag_is_not_a_login_page(vault_home):
    """The one that was measured wrong, kept as a case rather than as a
    story about a token list."""
    from dpc_client_core.dpc_agent.tools.browser import _page_wants_a_login

    assert _page_wants_a_login(SIGNED_IN_HTML) is False
    assert _page_wants_a_login(LOGIN_HTML) is True


def test_the_login_notice_does_not_promise_that_nothing_was_stored(vault_home):
    """The notice used to tell the person that a window left part-way through
    stores nothing and leaves the stored jar as it was. The write is
    unconditional now, so that sentence would be a lie told on our behalf."""
    from dpc_client_core.dpc_agent.tools.browser import _login_needed_notice

    notice = _login_needed_notice(TEST_DOMAIN)
    assert "stores nothing" not in notice
    assert "saved automatically as they make it" not in notice
    assert "saved as they go" in notice
    assert "STOP and wait" in notice
