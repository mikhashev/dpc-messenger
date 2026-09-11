"""The window is the act, and that is the whole rule.

One line decides everything here (Mike's call, 2026-09-11): a visible window
is ungated, anything not visible stays gated. The branch is taken on what the
page showed, never on whether the vault holds cookies — a jar full of guest
cookies over a dead session is exactly the signal that misled the design this
replaces.

What that buys and what it costs:
- a visible window reaches whatever the site delegates to, because a sign-in
  goes through identity providers and anti-bot gates that POST, and a gate
  narrow enough to be a gate blocks the login it was opened for;
- a headless fetch keeps the destination check and the per-site CDN manifest,
  because nobody is watching it;
- a headless fetch with no stored session is refused in words, because the
  alternative is downloading a login page into a window nobody can see and
  reporting it as the answer;
- the write scope never widened: a session stores only cookies inside its own
  `_etld1s`, visible or not.
"""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


# ── the doubles ─────────────────────────────────────────────────────────


class _SignedInPage:
    """A page the writeback accepts: no sign-in step, readable content.

    The stub used to be a bare `object()`. It cannot be any more — the
    writeback reads the page before it writes, and a page that answers
    nothing is a page that proves nothing."""

    url = "https://example/"

    def content(self):
        return (
            "<html><body><h1>Account</h1><p>Ordinary page content, long "
            "enough that an extractor returns something.</p></body></html>"
        )

    def is_closed(self):
        return False


class _StubContext:
    """Enough Playwright BrowserContext surface to drive _open()/close()."""

    def __init__(self, cookies_payload=None):
        self.added: list[list[dict]] = []
        self.routes: list[tuple] = []
        self.cookies_payload = list(cookies_payload or [])

    def add_cookies(self, cookies):
        self.added.append(list(cookies))

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def new_page(self):
        return _SignedInPage()

    def cookies(self):
        return list(self.cookies_payload)

    def close(self):
        pass


class _Route:
    def __init__(self, url, *, method="GET", resource_type="script",
                 nav=False, frame_url=None):
        frame = types.SimpleNamespace(url=frame_url) if frame_url else None
        self.request = types.SimpleNamespace(
            url=url, method=method, resource_type=resource_type,
            is_navigation_request=lambda: nav, frame=frame,
        )
        self.continued = False
        self.aborted = False

    def continue_(self):
        self.continued = True

    def abort(self):
        self.aborted = True


def _fresh_cookies(domain=TEST_DOMAIN, name="s"):
    return [{"name": name, "value": "v", "domain": f".{domain}", "path": "/",
             "expires": int(time.time()) + 3600, "secure": True,
             "httponly": True, "samesite": "Lax"}]


def _stale_cookies(domain=TEST_DOMAIN):
    return [{"name": "s", "value": "v", "domain": f".{domain}", "path": "/",
             "expires": int(time.time()) - 3600, "secure": True,
             "httponly": True, "samesite": "Lax"}]


def _pw(cookies):
    """Playwright shape — httpOnly/sameSite rather than the vault's snake."""
    return [
        {k: v for k, v in c.items() if k not in ("httponly", "samesite")}
        | {"httpOnly": True, "sameSite": "Lax"}
        for c in cookies
    ]


def _ctx(home: Path, agent_id="agent_a"):
    root = home / "agents" / agent_id
    root.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(agent_root=root)


def _audit(home: Path, agent_id="agent_a") -> list[dict]:
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _open_browser(monkeypatch, context, **kwargs):
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser, _active_camoufox_browsers,
    )

    seen: list[dict] = []

    class _StubBrowser:
        def new_context(self, **kw):
            seen.append(dict(kw))
            return context

        def on(self, *_a, **_kw):
            pass

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(), raising=False,
    )
    ab = AuthBrowser(agent_id="agent_a", **kwargs)
    try:
        ab._open()
    finally:
        _active_camoufox_browsers.discard(ab)
    return ab, seen


def _no_browser(monkeypatch):
    """Make every browse path explode rather than reach the network.

    A mutation that deletes a refusal turns a "refused" assertion into a live
    fetch against a real site, which then passes for the wrong reason."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    for name in ("_auth_browse_html", "_get_or_create_session_async",
                 "_get_or_create_fetch_session", "_browse_sync",
                 "_browse_with_camoufox"):
        monkeypatch.setattr(
            mod, name,
            lambda *a, _n=name, **kw: (_ for _ in ()).throw(
                AssertionError(f"{_n} was reached after a refusal")
            ),
        )


# ── the gate, and which side of it a window is on ───────────────────────


def test_a_visible_window_installs_no_gate_on_what_it_may_reach(vault_home):
    """The hosts measured as refused inside a window built for logging in:
    two identity providers as unlisted scripts, and an anti-bot gate on POST
    that no manifest can ever admit."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN], headed=True)
    for url, method, kind in (
        ("https://accounts.google.com/gsi/client", "GET", "script"),
        ("https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid.auth.js",
         "GET", "script"),
        ("https://edge.prelude.dev/v1/signals", "POST", "fetch"),
    ):
        route = _Route(url, method=method, resource_type=kind,
                       frame_url=f"https://{TEST_DOMAIN}/login")
        ab._domain_route_gate(route)
        assert route.continued is True, url
        assert route.aborted is False, url


def test_a_headless_session_is_still_gated(vault_home):
    """The other half of the same sentence. Without it the rollback would
    have deleted the destination check rather than scoped it."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN], headed=False)
    route = _Route("https://accounts.google.com/gsi/client",
                   frame_url=f"https://{TEST_DOMAIN}/home")
    ab._domain_route_gate(route)

    assert route.aborted is True
    assert route.continued is False


def test_the_pre_navigation_check_agrees_with_the_gate(vault_home):
    """Two layers disagreeing is how a hole hides — here it would be a gate
    that admits an identity provider while the check in front refuses."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    visible = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN], headed=True)
    visible._check_domain("https://accounts.google.com/signin")  # must not raise

    headless = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN], headed=False)
    with pytest.raises(ValueError):
        headless._check_domain("https://accounts.google.com/signin")


def test_a_visible_window_starts_with_the_vaults_own_cookies(
    vault_home, monkeypatch,
):
    """The rolled-back design opened the login window on a clean profile, so
    a person who was already signed in had to sign in again. The window now
    arrives holding whatever is stored for its scope."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    ctx = _StubContext()

    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True,
    )

    assert ab._start_clean is False
    assert ctx.added and [c["name"] for c in ctx.added[0]] == ["s"]


def test_an_ungated_window_still_stores_only_its_own_site(
    vault_home, monkeypatch,
):
    """The read side is open; the write side is not. A window that walks to
    an identity provider carries nothing of it into the vault."""
    from dpc_client_core import web_auth

    ctx = _StubContext(cookies_payload=_pw(
        _fresh_cookies(TEST_DOMAIN, name="auth_token")
        + _fresh_cookies("google.com", name="SID")
    ))
    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True,
    )
    ab._persist_session_cookies()

    assert [r["domain"] for r in web_auth.list_domains("agent_a")] == [TEST_DOMAIN]
    assert web_auth.load_cookies("agent_a", "google.com") is None
    # The jar's contents, not only its name: a foreign cookie filed under the
    # site's own key passes every assertion above and is the same leak.
    assert [c["name"] for c in web_auth.load_cookies("agent_a", TEST_DOMAIN)] == [
        "auth_token"
    ]


def test_no_session_loads_a_saved_browser_state(vault_home, monkeypatch):
    """The defect the whole day started from: `browser_state.json` had grown
    into a second identity store — 230 cookies over 55 domains, a live session
    token among them, put there by nobody's decision. A session's identity is
    the vault jar for its own scope, and `storage_state` must reach
    `new_context` from nowhere."""
    state = vault_home / "agents" / "agent_a" / "browser_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"cookies": [{"name": "auth_token", "value": "someone_else",
                                 "domain": f".{TEST_DOMAIN}", "path": "/"}],
                    "origins": []}),
        encoding="utf-8",
    )

    for kwargs in ({"domains": [TEST_DOMAIN]},
                   {"domains": [TEST_DOMAIN], "headed": True},
                   {"domains": [], "anonymous": True}):
        ctx = _StubContext()
        _ab, seen = _open_browser(monkeypatch, ctx, **kwargs)
        assert "storage_state" not in seen[0], kwargs
        assert ctx.added in ([], [[]]), kwargs


# ── where an ungated window went ────────────────────────────────────────


def test_where_an_ungated_window_went_is_recorded_with_its_initiator(
    vault_home,
):
    """Enforcing nothing is not the same as seeing nothing — and in a window
    the agent can drive, `initiator` is the only field separating "the site
    called its identity provider" from "something went out on its own"."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN], headed=True)
    ab._domain_route_gate(_Route(
        "https://accounts.google.com/gsi/client",
        frame_url=f"https://{TEST_DOMAIN}/login",
    ))

    row = next(r for r in _audit(vault_home)
               if r.get("action") == AuthBrowser.GATE_ACTION_VISIBLE_PASSTHROUGH)
    assert row["dest_host"] == "accounts.google.com"
    assert row["initiator"] == f"https://{TEST_DOMAIN}/login"
    assert row["result"] == "ok"


def test_a_request_with_no_readable_frame_records_an_empty_initiator(
    vault_home,
):
    """The field must be absent honestly rather than invented: a request with
    no frame behind it is what "went out on its own" looks like."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN], headed=True)
    ab._domain_route_gate(_Route("https://elsewhere.example/beacon"))

    row = next(r for r in _audit(vault_home)
               if r.get("action") == AuthBrowser.GATE_ACTION_VISIBLE_PASSTHROUGH)
    assert row["initiator"] == ""


# ── the headless refusal ────────────────────────────────────────────────


def _browse(ctx, **kw):
    from dpc_client_core.dpc_agent.tools import browser as mod

    return asyncio.run(mod.browse_page(
        ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=TEST_DOMAIN, **kw
    ))


def test_a_headless_browse_with_no_session_refuses_in_words(
    vault_home, monkeypatch,
):
    _no_browser(monkeypatch)

    answer = _browse(_ctx(vault_home))

    assert "No stored session" in answer
    assert TEST_DOMAIN in answer
    assert "keep_open=true" in answer
    assert [r["status"] for r in _audit(vault_home)] == ["auth_denied:no_session"]


def test_a_jar_whose_cookies_have_all_expired_is_no_session(
    vault_home, monkeypatch,
):
    """`has_cookies` was the old question and it says yes here. The one that
    decides is whether anything unexpired is left to send."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _stale_cookies())
    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"] is True
    _no_browser(monkeypatch)

    answer = _browse(_ctx(vault_home))

    assert "No stored session" in answer


def test_a_headless_browse_with_a_session_goes_through(vault_home, monkeypatch):
    """A gate that refuses everything is not a gate, it is an outage. And no
    UI is consulted on this path any more — the context carries no service."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as mod

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    monkeypatch.setattr(
        mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    _browse(_ctx(vault_home))

    assert [r["status"] for r in _audit(vault_home)] == [200]


def test_a_visible_browse_with_no_session_is_not_refused(
    vault_home, monkeypatch,
):
    """The refusal belongs to the blind path only. A window with nothing
    stored is precisely how a person signs in for the first time."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    async def _fake_session(agent_id, domains, headed):
        return object()

    async def _fake_nav(session, agent_id, url, domains):
        return session

    async def _fake_run(session, method, *a, **kw):
        return "<html><body><h1>signed in</h1></body></html>"

    monkeypatch.setattr(mod, "_get_or_create_session_async", _fake_session)
    monkeypatch.setattr(mod, "_navigate_with_recovery", _fake_nav)
    monkeypatch.setattr(mod, "_run_in_session", _fake_run)

    answer = _browse(_ctx(vault_home), keep_open=True)

    assert "No stored session" not in answer
    assert [r["status"] for r in _audit(vault_home)] == [200]


def test_a_visible_page_asking_for_a_login_tells_the_agent_to_say_so(
    vault_home, monkeypatch,
):
    """The coordination channel is the chat, and a tool can only put words in
    the model's hands. So the words have to name who acts next."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    async def _fake_session(agent_id, domains, headed):
        return object()

    async def _fake_nav(session, agent_id, url, domains):
        return session

    async def _fake_run(session, method, *a, **kw):
        return (
            '<html><body><form>'
            '<input type="text" name="user">'
            '<input type="password" name="pass">'
            "</form></body></html>"
        )

    monkeypatch.setattr(mod, "_get_or_create_session_async", _fake_session)
    monkeypatch.setattr(mod, "_navigate_with_recovery", _fake_nav)
    monkeypatch.setattr(mod, "_run_in_session", _fake_run)

    answer = _browse(_ctx(vault_home), keep_open=True)

    assert "A LOGIN IS NEEDED" in answer
    assert "in the chat" in answer
    assert "wait for their reply" in answer
    assert "login_page_shown" in [r.get("status") for r in _audit(vault_home)]


def test_a_page_with_content_says_nothing_about_a_login(
    vault_home, monkeypatch,
):
    """The counterpart, or the notice would fire on every page and the agent
    would learn to ignore it."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    async def _fake_session(agent_id, domains, headed):
        return object()

    async def _fake_nav(session, agent_id, url, domains):
        return session

    async def _fake_run(session, method, *a, **kw):
        return "<html><body><h1>Your orders</h1><p>Two items</p></body></html>"

    monkeypatch.setattr(mod, "_get_or_create_session_async", _fake_session)
    monkeypatch.setattr(mod, "_navigate_with_recovery", _fake_nav)
    monkeypatch.setattr(mod, "_run_in_session", _fake_run)

    answer = _browse(_ctx(vault_home), keep_open=True)

    assert "A LOGIN IS NEEDED" not in answer


# ── nothing grants, so nothing can be forged ────────────────────────────


def test_no_approval_survives_anywhere_in_the_vault(vault_home):
    """The rolled-back mechanism, stated as an absence: a jar carries cookies
    and timestamps, and there is no field a browser could stamp to authorise
    itself."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())

    # The stored entry itself, not only the two views of it: a field written
    # to disk that no reader consults today is a half-restored mechanism
    # waiting for the reader to come back.
    entry = web_auth._load_vault("agent_a")["domains"][TEST_DOMAIN]
    assert set(entry) == {"cookies", "authenticated_at", "last_used_at"}

    status = web_auth.get_auth_status("agent_a", TEST_DOMAIN)
    assert "approved" not in status
    row = web_auth.list_domains("agent_a")[0]
    assert "approved" not in row
    for gone in ("is_approved", "get_approval", "record_approval",
                 "identity_marks", "APPROVAL_VIA_LOGIN_WINDOW"):
        assert not hasattr(web_auth, gone), gone


def test_the_login_window_tool_is_gone(vault_home):
    """Merged into the ordinary visible window. A tool still registered would
    mean two ways to open a browser and one design describing each."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    names = {t.name for t in mod.get_tools()}
    assert "open_login_window" not in names
    assert "browse_page" in names
    for gone in ("open_login_window", "get_login_windows",
                 "get_pending_auth_approvals", "_ask_human_to_approve"):
        assert not hasattr(mod, gone), gone
