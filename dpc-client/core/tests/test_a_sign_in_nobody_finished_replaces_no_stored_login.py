"""A window that did not finish a sign-in must not replace the stored one.

`save_cookies` replaces a jar rather than merging into it, and a site hands
its guest cookies to any window that arrives without a session. So the
writeback after every navigate and at close was able to open a site whose
login was already stored, collect the guest jar the site handed the window,
and write it over the real one — no warning, and nothing to undo it with.

The condition on the write is now the page, evaluated inside
`_persist_session_cookies` so that both call sites are covered by
construction: the page must show no sign-in step AND yield readable content.
Absence of a login form is not presence of a session — a challenge screen, a
blank render and a 404 all have no password field on them.

Two properties are measured here rather than asserted in prose:

* byte-for-byte equality of the vault file. A Fernet blob is re-randomised
  on every write, so an unchanged file proves no write was attempted at all,
  not merely that the contents came out equal;
* the jar the write displaced is still reachable, because every guard in
  front of the write is a judgement about a page and a judgement can be
  wrong.

Nothing here touches a network or a browser: the context and the page are
stubs, and the refusal paths are driven directly rather than through
`browse_page`, so a guard removed by a mutation cannot turn a test into a
live fetch.
"""

import asyncio
import json
import time

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


AGENT = "agent_a"

CONTENT_HTML = (
    "<html><body><h1>Your timeline</h1><p>Ordinary page content, long enough "
    "that an extractor returns something rather than nothing.</p></body></html>"
)
LOGIN_HTML = (
    '<html><body><form><input name="username"><input type="password" '
    'name="password"></form></body></html>'
)
TWO_FACTOR_HTML = (
    "<html><body><h1>Check your phone</h1>"
    '<form action="/i/flow/two_factor_code"><input type="text" '
    'autocomplete="one-time-code" name="code"></form></body></html>'
)
CHALLENGE_HTML = (
    '<html><body><div id="cf-challenge-running">Checking your browser '
    "before you continue</div></body></html>"
)
BLANK_HTML = "<html><body><div></div></body></html>"


# ── doubles ──────────────────────────────────────────────────────────────


class _Page:
    """A page that answers `content()`, or refuses to."""

    def __init__(self, html=CONTENT_HTML, raises=None):
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
    and an anti-bot cookie. Both are in scope for the jar they would
    replace, and one of them is long, opaque and httpOnly — which is why no
    guard here looks at a cookie at all."""
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


def _session_cookies(domain=TEST_DOMAIN, value="real-session-token"):
    return [
        {
            "name": "auth_token", "value": value, "domain": f".{domain}",
            "path": "/", "expires": time.time() + 86400,
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


def _browser(html=CONTENT_HTML, raises=None, cookies=None, domains=None):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id=AGENT, domains=domains or [TEST_DOMAIN], headed=True,
    )
    ab._context = _Context(_to_playwright(cookies or _guest_cookies()))
    ab._page = _Page(html=html, raises=raises)
    return ab


# ── the jar survives a sign-in nobody finished ───────────────────────────


@pytest.mark.parametrize(
    "html, expected_reason",
    [
        (LOGIN_HTML, "login_page_shown"),
        (TWO_FACTOR_HTML, "login_page_shown"),
        (CHALLENGE_HTML, "login_page_shown"),
        (BLANK_HTML, "page_shows_no_content"),
    ],
    ids=["login_form", "two_factor_code", "anti_bot_challenge", "blank_render"],
)
def test_a_window_that_did_not_finish_a_sign_in_leaves_the_jar_byte_for_byte(
    vault_home, html, expected_reason,
):
    """Four ways to end a window without a session behind it, one outcome on
    disk: nothing happened.

    The two-factor case is the one that was measured: it carries no password
    field, so a guard that only knew about password fields read it as content
    and wrote the guest jar over the real login."""
    _seed_stored_login()
    before = _vault_bytes(vault_home)
    assert before

    ab = _browser(html=html)
    assert ab._persist_session_cookies() == expected_reason

    assert _vault_bytes(vault_home) == before


def test_a_window_that_did_not_finish_a_sign_in_writes_nothing_at_close(
    vault_home,
):
    """The close is where the loss happened, so the close is pinned on its
    own — `close()` reaches the same condition, it does not carry one."""
    _seed_stored_login()
    before = _vault_bytes(vault_home)

    ab = _browser(html=TWO_FACTOR_HTML)
    ab.close()

    assert _vault_bytes(vault_home) == before


def test_a_page_that_shows_content_does_write(vault_home):
    """The refusal must not be the only outcome: a finished sign-in still
    reaches the vault, or the flow stores nothing at all and the window is
    pointless."""
    from dpc_client_core import web_auth

    ab = _browser(html=CONTENT_HTML, cookies=_session_cookies(value="fresh"))
    assert ab._persist_session_cookies() == "page_shows_content"

    saved = web_auth.load_cookies(AGENT, TEST_DOMAIN)
    assert [c["name"] for c in saved] == ["auth_token"]
    assert saved[0]["value"] == "fresh"


# ── a page that cannot be read decides nothing, so it writes nothing ─────


@pytest.mark.parametrize(
    "kwargs, expected_reason",
    [
        ({"raises": RuntimeError("Target page, context or browser has been closed")},
         "page_unreadable:RuntimeError"),
        ({"raises": AttributeError("page is gone")}, "page_unreadable:AttributeError"),
    ],
    ids=["browser_closed_underneath", "page_object_broken"],
)
def test_a_page_that_cannot_be_read_leaves_the_jar_byte_for_byte(
    vault_home, kwargs, expected_reason,
):
    """"Cannot tell" resolves to "do not write". Answering it the other way
    is how the loss happened, and the cost of this direction is one snapshot
    skipped for a browser that is already dying — whose next navigate, if it
    has one, writes it anyway."""
    _seed_stored_login()
    before = _vault_bytes(vault_home)

    ab = _browser(**kwargs)
    assert ab._persist_session_cookies() == expected_reason

    assert _vault_bytes(vault_home) == before


def test_a_session_with_no_page_at_all_writes_nothing(vault_home):
    _seed_stored_login()
    before = _vault_bytes(vault_home)

    ab = _browser()
    ab._page = None
    assert ab._persist_session_cookies() == "page_unreadable:no_page"

    assert _vault_bytes(vault_home) == before


# ── a refusal is said out loud, and written down ─────────────────────────


def test_a_declined_write_is_recorded_in_the_audit_log(vault_home):
    """A refusal nobody can see is indistinguishable from the silent
    overwrite it replaces."""
    _seed_stored_login()

    ab = _browser(html=LOGIN_HTML)
    ab._persist_session_cookies()

    declined = [
        r for r in _audit_rows(vault_home)
        if r.get("action") == "cookie_writeback"
    ]
    assert len(declined) == 1
    assert declined[0]["result"] == "declined"
    assert declined[0]["reason"] == "login_page_shown"
    assert declined[0]["domain"] == TEST_DOMAIN


def test_a_declined_write_is_announced_when_the_agent_closes_the_window(
    vault_home, monkeypatch,
):
    """`browser_close` is the one close with a tool result to speak in, and
    the words are the flow's own: finish the sign-in, then reply."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ab = _browser(html=TWO_FACTOR_HTML)

    async def _direct(session, method_name, *args, **kwargs):
        kwargs.pop("_touch", None)
        kwargs.pop("_timeout", None)
        return getattr(session, method_name)(*args, **kwargs)

    monkeypatch.setattr(browser_mod, "_run_in_session", _direct)
    monkeypatch.setitem(browser_mod._active_browser_sessions, AGENT, ab)

    class _Ctx:
        agent_root = type("R", (), {"name": AGENT})()

    answer = asyncio.run(browser_mod.browser_close(_Ctx()))

    assert "THE SIGN-IN WAS NOT SAVED" in answer
    assert TEST_DOMAIN in answer
    assert "login_page_shown" in answer
    assert "STOP and wait" in answer


def test_a_written_snapshot_announces_nothing(vault_home, monkeypatch):
    """The counterpart, so the notice cannot be a constant: a close that did
    write says only that it closed."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ab = _browser(html=CONTENT_HTML, cookies=_session_cookies())

    async def _direct(session, method_name, *args, **kwargs):
        kwargs.pop("_touch", None)
        kwargs.pop("_timeout", None)
        return getattr(session, method_name)(*args, **kwargs)

    monkeypatch.setattr(browser_mod, "_run_in_session", _direct)
    monkeypatch.setitem(browser_mod._active_browser_sessions, AGENT, ab)

    class _Ctx:
        agent_root = type("R", (), {"name": AGENT})()

    answer = asyncio.run(browser_mod.browser_close(_Ctx()))

    assert answer == "Browser session closed"


# ── the insurance that does not depend on a judgement ────────────────────


def test_a_replaced_jar_is_still_reachable(vault_home):
    """Every guard in front of the write is an opinion about a page. This is
    the part that holds when the opinion is wrong."""
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


# ── what the agent is told must stay true ────────────────────────────────


def test_the_login_notice_does_not_promise_an_unconditional_save(vault_home):
    """The notice used to say the sign-in is saved automatically as it is
    made. With the write now conditional, that sentence would have the agent
    tell the person something untrue."""
    from dpc_client_core.dpc_agent.tools.browser import _login_needed_notice

    notice = _login_needed_notice(TEST_DOMAIN)
    assert "saved automatically as they make it" not in notice
    assert "finished" in notice
