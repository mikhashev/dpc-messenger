"""A sign-in made in a kept-open window is saved when it is made.

The incident (agent Forge, 2026-11-27): browse_page(keep_open=true)
WITHOUT use_auth opened its window with domains=[], so
`_persist_session_cookies` answered `no_scope` and stored nothing — while
the answer still carried `_login_needed_notice`, telling the person to
sign in into a window that could never save it. The audit shows navigate
rows and a close row with no cookie_writeback beside any of them.

Three things now hold, and each has its test here:

* the window's scope is the registrable domain of its URL, exactly as a
  use_auth window's would be — the jar is loaded at open and written back
  on navigate and close, and the approval the call carries is unchanged:
  the plain-fetch card still asks only to open the page, and "logged in
  as the owner" stays what `use_auth` alone says;
* the window probe stores what the window holds while it stays open, with
  no navigate and no close behind the write — and an unchanged jar is not
  rewritten every half minute, so `authenticated_at` moves only when the
  cookies did (THE-VAULT-STAMPS-A-FRESH-LOGIN-EVERY-TIME-THE-BROWSER-
  CLOSES);
* the guest cookies a not-yet-signed-in window holds do not replace a
  stored login on that periodic path, while a real re-authentication —
  which still carries the stored jar's own cookie names — does update it.

Nothing here touches a network or launches a browser: the context and the
page are stubs, every path is driven directly or through the probe, and
the one browse_page-driven test monkeypatches every call that could open
anything.
"""

from __future__ import annotations

import asyncio
import json
import time
import types

import pytest

from dpc_client_core.dpc_agent.tools import browser as B

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


AGENT = "agent_window"

LOGIN_HTML = (
    '<html><body><form><input name="username"><input type="password" '
    'name="password"></form></body></html>'
)
SIGNED_IN_HTML = (
    "<html><body><h1>Your timeline</h1><p>Ordinary page content, long "
    "enough that an extractor returns something.</p></body></html>"
)


# ── doubles ──────────────────────────────────────────────────────────────


class _Page:
    """A page that answers the probe (`title`) and the audit (`url`)."""

    def __init__(self, html=SIGNED_IN_HTML):
        self._html = html
        self.url = f"https://{TEST_DOMAIN}/home"

    def title(self):
        return "a title"

    def content(self):
        return self._html

    def is_closed(self):
        return False


class _Context:
    """A context whose jar is whatever the test says the window holds."""

    def __init__(self, cookies_payload):
        self.cookies_payload = list(cookies_payload)
        self.cookies_calls = 0
        self.added: list[list[dict]] = []

    def add_cookies(self, cookies):
        self.added.append(list(cookies))

    def route(self, pattern, handler):
        pass

    def on(self, event, handler):
        pass

    def new_page(self):
        return _Page()

    def cookies(self):
        self.cookies_calls += 1
        return list(self.cookies_payload)

    def close(self):
        pass


def _guest_cookies(domain=TEST_DOMAIN):
    """What a site hands a window nobody has signed into."""
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


def _pw(cookies):
    """Playwright shape — httpOnly/sameSite rather than the vault's snake."""
    return [
        {k: v for k, v in c.items() if k not in ("httponly", "samesite")}
        | {"httpOnly": c.get("httponly", False),
           "sameSite": c.get("samesite", "Lax")}
        for c in (dict(x) for x in cookies)
    ]


@pytest.fixture(autouse=True)
def clean_registry():
    before = dict(B._active_browser_sessions)
    B._active_browser_sessions.clear()
    yield
    B._active_browser_sessions.clear()
    B._active_browser_sessions.update(before)


def _window(cookies, *, domains=None, html=SIGNED_IN_HTML):
    """A headed, scoped session over a stub context — never opened."""
    ab = B.AuthBrowser(
        agent_id=AGENT, domains=domains or [TEST_DOMAIN], headed=True,
    )
    ab._context = _Context(_pw(cookies))
    ab._page = _Page(html=html)
    return ab


def _seed_stored_login(value="the-real-one"):
    from dpc_client_core import web_auth

    web_auth.save_cookies(AGENT, TEST_DOMAIN, _session_cookies(value=value))


def _stored_login():
    from dpc_client_core import web_auth

    return web_auth.load_cookies(AGENT, TEST_DOMAIN)


def _vault_bytes(home) -> bytes:
    from dpc_client_core import web_auth

    path = web_auth._vault_path(AGENT)
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


def _counting_save(monkeypatch):
    """Wrap `web_auth.save_cookies` (as browser.py sees it) with a counter."""
    from dpc_client_core import web_auth

    calls: list[tuple] = []
    real = web_auth.save_cookies

    def _counted(agent_id, domain, cookies, **kw):
        calls.append((domain, tuple(sorted(c.get("name", "") for c in cookies))))
        return real(agent_id, domain, cookies, **kw)

    monkeypatch.setattr(web_auth, "save_cookies", _counted)
    return calls


# ── the scope: the URL's own site, and the approval unchanged ───────────


def _keep_open_browse(monkeypatch, url, *, html=SIGNED_IN_HTML):
    """Drive the keep_open-without-use_auth branch with every opener faked."""
    opened: list[list[str]] = []

    async def _fake_session(agent_id, domains, headed):
        opened.append(list(domains))
        return types.SimpleNamespace(_etld1s=set(domains))

    async def _fake_nav(session, agent_id, nav_url, domains):
        return session

    async def _fake_run(session, method_name, *args, **kwargs):
        if method_name == "get_page_html":
            return html
        if method_name == "page_url":
            return url
        return None

    monkeypatch.setattr(B, "_get_or_create_session_async", _fake_session)
    monkeypatch.setattr(B, "_navigate_with_recovery", _fake_nav)
    monkeypatch.setattr(B, "_run_in_session", _fake_run)

    ctx = types.SimpleNamespace(
        agent_root=types.SimpleNamespace(name=AGENT),
    )
    return asyncio.run(B.browse_page(ctx, url=url, keep_open=True)), opened


def test_a_kept_open_window_without_use_auth_is_scoped_to_the_urls_site(
    vault_home, monkeypatch,
):
    """The incident's first half: `domains=[]` meant no jar at all — nothing
    loaded at open, nothing written back, ever. The scope is now the URL's
    registrable domain, taken from the URL itself."""
    answer, opened = _keep_open_browse(
        monkeypatch, f"https://www.{TEST_DOMAIN}/watch?v=1",
    )

    assert opened == [[TEST_DOMAIN]]
    # The header says where the window's writes go, as a fact of the call.
    assert f"save scope {TEST_DOMAIN}" in answer


def test_the_plain_fetch_card_is_not_upgraded_to_the_owners_login(
    vault_home,
):
    """The approval the call carries is unchanged by the scope: `use_auth`
    is what says "logged in as the owner", and a window opened without it
    still asks only to open the page."""
    decision = B._browse_page_approval(
        None, {"url": f"https://{TEST_DOMAIN}/my", "keep_open": True},
    )
    assert "logged in as the owner" not in decision.details
    assert "open " in decision.details

    authed = B._browse_page_approval(
        None, {"url": f"https://{TEST_DOMAIN}/my", "use_auth": TEST_DOMAIN},
    )
    assert "logged in as the owner" in authed.details


def test_a_scoped_kept_open_window_writes_its_sign_in_back(vault_home):
    """The incident's second half, joined to the first: a session created
    with the scope the branch now passes stores what the window holds —
    the writeback that never ran because the scope was empty."""
    from dpc_client_core import web_auth

    _seed_stored_login(value="the-old-one")
    ab = _window(_session_cookies(value="signed-in-by-hand"))

    assert ab._persist_session_cookies() == "written"
    assert _stored_login()[0]["value"] == "signed-in-by-hand"
    # And the stored jar loads at open, exactly as a use_auth window's
    # would: the person already signed in is not asked to again.
    ctx = _Context([])
    ab2 = B.AuthBrowser(agent_id=AGENT, domains=[TEST_DOMAIN], headed=True)
    ab2._context, ab2._page = ctx, _Page()
    ab2._inject_vault_cookies(skip_missing=True)
    assert [c["name"] for c in ctx.added[0]] == ["auth_token"]


def test_a_kept_open_window_on_an_unscopable_address_says_the_truth(
    vault_home, monkeypatch,
):
    """An IP literal names no registrable domain, so there is no jar to key
    a sign-in to: the window opens unscoped, and the notice says the
    sign-in will not be saved rather than promising a save that cannot
    happen — the promise this branch used to make for every URL."""
    answer, opened = _keep_open_browse(
        monkeypatch, "http://192.0.2.1/login", html=LOGIN_HTML,
    )

    assert opened == [[]]
    assert "A LOGIN IS NEEDED" in answer
    assert "will NOT be saved" in answer
    assert "saved as they go" not in answer


# ── the probe: stored while the window stays open, and only on change ───


def _sweep(ab):
    B._active_browser_sessions[AGENT] = ab
    closed = []
    ab.close = lambda: closed.append(True)  # type: ignore[method-assign]
    released = asyncio.run(B.sweep_closed_windows())
    return released, closed


def test_the_window_probe_stores_a_sign_in_while_the_window_stays_open(
    vault_home, monkeypatch,
):
    """No navigate, no close, no agent call: the person signs in, the page
    sets its cookies, and the half-minute probe carries them to the vault.
    The window is still there afterwards, and still registered."""
    save_calls = _counting_save(monkeypatch)
    ab = _window(_session_cookies(value="signed-in-by-hand"))

    released, closed = _sweep(ab)

    assert released == 0
    assert closed == []
    assert B._active_browser_sessions.get(AGENT) is ab
    assert len(save_calls) == 1
    assert _stored_login()[0]["value"] == "signed-in-by-hand"
    rows = [r for r in _audit_rows(vault_home)
            if r.get("action") == "cookie_writeback"]
    assert [r["result"] for r in rows] == ["ok"]


def test_an_unchanged_jar_is_not_rewritten_every_probe(
    vault_home, monkeypatch,
):
    """The stamp says when these bytes were written. A probe every half
    minute that rewrote an unchanged jar would move it on nothing — a
    fresh login stamped on every tick. Byte-for-byte equality of the vault
    file is the measurement: a Fernet blob is re-randomised on every
    write, so an unchanged file proves no write was attempted at all."""
    save_calls = _counting_save(monkeypatch)
    ab = _window(_session_cookies(value="signed-in-by-hand"))

    _sweep(ab)
    assert len(save_calls) == 1
    before = _vault_bytes(vault_home)
    assert before

    _sweep(ab)

    assert len(save_calls) == 1, "the unchanged jar was written again"
    assert _vault_bytes(vault_home) == before
    rows = [r for r in _audit_rows(vault_home)
            if r.get("action") == "cookie_writeback"]
    assert len(rows) == 1, "an unchanged skip is not a writeback, and not a row"


def test_a_jar_that_changed_reaches_the_vault_on_the_next_probe(
    vault_home, monkeypatch,
):
    """The other half, or the skip would be a stuck door: the person
    finishes the sign-in, the snapshot differs, the probe writes it."""
    save_calls = _counting_save(monkeypatch)
    ab = _window(_session_cookies(value="half-way"))

    _sweep(ab)
    ab._context.cookies_payload = _pw(
        _session_cookies(value="all-the-way-through")
    )
    _sweep(ab)

    assert len(save_calls) == 2
    assert _stored_login()[0]["value"] == "all-the-way-through"


def test_a_failing_writeback_neither_breaks_the_probe_nor_closes_the_window(
    vault_home,
):
    """Best-effort by contract: the writeback runs inside the sweep, and an
    exception in it may not take the loop or the window down — the next
    tick asks again."""

    class _BrokenContext(_Context):
        def cookies(self):
            raise RuntimeError("context wedged mid-save")

    ab = B.AuthBrowser(agent_id=AGENT, domains=[TEST_DOMAIN], headed=True)
    ab._context = _BrokenContext([])
    ab._page = _Page()

    released, closed = _sweep(ab)

    assert released == 0
    assert closed == []
    assert B._active_browser_sessions.get(AGENT) is ab


# ── guest cookies must not spend a stored login ──────────────────────────


def test_guest_cookies_do_not_replace_a_stored_login_on_the_probe(
    vault_home, monkeypatch,
):
    """A window that has not been signed into yet holds the site's guest
    cookies, and `save_cookies` refuses only an empty snapshot — so a
    periodic writeback would have overwritten the stored login with them.
    On the periodic path it declines instead, and a decline that persists
    across probes is said once."""
    _seed_stored_login(value="the-real-one")
    save_calls = _counting_save(monkeypatch)
    before = _vault_bytes(vault_home)
    ab = _window(_guest_cookies())

    _sweep(ab)
    _sweep(ab)

    assert len(save_calls) == 2, "the probe asked; the vault refused"
    assert _vault_bytes(vault_home) == before
    assert _stored_login()[0]["value"] == "the-real-one"
    declined = [
        r for r in _audit_rows(vault_home)
        if r.get("action") == "cookie_writeback" and r["result"] == "declined"
    ]
    assert len(declined) == 1
    assert declined[0]["reason"] == f"drops_the_stored_session:{TEST_DOMAIN}"
    # The decline is housekeeping, not a lost sign-in: nothing is queued
    # for the next tool answer to say.
    assert ab._last_writeback_decline is None


def test_a_real_re_authentication_updates_the_stored_jar_on_the_probe(
    vault_home, monkeypatch,
):
    """The neighbouring input that must still pass: a re-authentication
    still carries the stored jar's own cookie names, so it writes — the
    guard reads names the jar itself holds, never a guess about which of
    them carries the login."""
    _seed_stored_login(value="the-old-login")
    save_calls = _counting_save(monkeypatch)
    ab = _window(_guest_cookies() + _session_cookies(value="the-new-login"))

    _sweep(ab)

    assert len(save_calls) == 1
    jar = {c["name"]: c["value"] for c in _stored_login()}
    assert jar["auth_token"] == "the-new-login"


def test_an_ordinary_writeback_still_replaces_the_jar(
    vault_home, monkeypatch,
):
    """Navigate and close keep the semantics their tests pin: the ordinary
    writeback is how a sign-in is saved as it is made, it displaces what
    is there, and `previous` stays behind it."""
    _seed_stored_login(value="the-real-one")
    save_calls = _counting_save(monkeypatch)
    ab = _window(_guest_cookies())

    assert ab._persist_session_cookies() == "written"
    assert len(save_calls) == 1

    assert [c["name"] for c in _stored_login()] == ["guest_id", "cf_clearance"]
    from dpc_client_core import web_auth

    assert web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN) is True
    assert _stored_login()[0]["value"] == "the-real-one"
