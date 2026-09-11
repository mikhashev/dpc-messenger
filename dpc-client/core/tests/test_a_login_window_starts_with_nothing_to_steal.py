"""The login window is self-securing, and that rests on two facts.

It starts clean — no `storage_state`, no vault cookies injected — so there
is nothing in it to take, which is what lets the gate inside it be widened
far enough for a sign-in to complete. And it is scoped to one site, so the
only cookies it can write are that site's.

What those two facts do NOT establish is that anybody logged in: the first
live run collected a full jar of anonymous guest cookies. The approval is
asked for and lives in
`test_an_approval_is_a_human_act_not_a_cookie.py`.

Both halves are load-bearing and they pull in opposite directions, so they
live on separate fields: `_start_clean` decides what is loaded, `_domains`
decides what may be written. Fusing them — passing `domains=[S]` to get the
route gate and the write scope — would have pulled S's stored cookies into
the window and destroyed the argument. That is what the first three tests
here are for.
"""

import asyncio
import json
import logging
import time
import types
from pathlib import Path

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


def _fresh_cookies(domain=TEST_DOMAIN):
    return [{"name": "s", "value": "v", "domain": f".{domain}", "path": "/",
             "expires": int(time.time()) + 3600, "secure": True,
             "httponly": True, "samesite": "Lax"}]


def _pw_cookies(domain=TEST_DOMAIN):
    """Playwright shape — what a live context hands back."""
    return [{"name": "s", "value": "v", "domain": f".{domain}", "path": "/",
             "expires": int(time.time()) + 3600, "secure": True,
             "httpOnly": True, "sameSite": "Lax"}]


def _read_audit(home: Path, agent_id: str) -> list[dict]:
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


class _Ctx:
    def __init__(self, home: Path, agent_id="agent_a", local_api=None):
        self.agent_root = home / "agents" / agent_id
        self.agent_root.mkdir(parents=True, exist_ok=True)
        self.dpc_service = types.SimpleNamespace(local_api=local_api)


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
        return object()

    def cookies(self):
        return list(self.cookies_payload)

    def storage_state(self, path=None):
        return {"cookies": list(self.cookies_payload), "origins": []}


def _stub_camoufox(monkeypatch, context):
    """Patch Camoufox so _open() builds against `context`, and report the
    kwargs new_context() was called with."""
    seen: list[dict] = []

    class _StubBrowser:
        def new_context(self, **kwargs):
            seen.append(dict(kwargs))
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
    return seen


def _open_browser(monkeypatch, context, **kwargs):
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser, _active_camoufox_browsers,
    )

    seen = _stub_camoufox(monkeypatch, context)
    ab = AuthBrowser(agent_id="agent_a", **kwargs)
    try:
        ab._open()
    finally:
        _active_camoufox_browsers.discard(ab)
    return ab, seen


# ── cleanliness of the start ────────────────────────────────────────────


def test_a_login_window_loads_no_stored_cookies(vault_home, monkeypatch):
    """The one that would have been silently false: `domains=[S]` is what
    drives `_inject_vault_cookies`, so a login window scoped to S would have
    opened holding S's existing login."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    # Both sources of a stale login have to be present, or the assertions
    # below pass on their absence: without this file the storage_state
    # branch is never reached at all.
    state = vault_home / "agents" / "agent_a" / "browser_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"cookies": [{"name": "old"}], "origins": []}),
                     encoding="utf-8")
    ctx = _StubContext()

    ab, seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )

    assert ctx.added == [], "a login window must inject no vault cookies"
    assert "storage_state" not in seen[0], "and load no saved browser state"
    assert ab._start_clean is True


def test_an_ordinary_session_still_loads_its_stored_cookies(vault_home, monkeypatch):
    """The counterpart: cleanliness belongs to the login window alone, or
    every authenticated browse would silently lose its login."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    ctx = _StubContext()

    ab, _seen = _open_browser(monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True)

    assert ab._start_clean is False
    assert ctx.added and ctx.added[0][0]["name"] == "s"


def test_cleanliness_and_write_scope_are_separate_fields(vault_home):
    """Stated as an invariant, because fusing them is the trap: the login
    window is clean AND scoped, the JS-fallback fetch is clean and scoped to
    nothing, the ordinary session is scoped and not clean."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    login = AuthBrowser(
        agent_id="agent_a", domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    assert login._start_clean is True
    assert login._etld1s, "the route gate and the write scope both need this"

    fetch = AuthBrowser(agent_id="agent_a", domains=[], anonymous=True)
    assert fetch._start_clean is True
    assert fetch._login_window is False

    ordinary = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    assert ordinary._start_clean is False
    assert ordinary._login_window is False


def test_the_route_gate_is_installed_on_a_login_window(vault_home, monkeypatch):
    """`domains=[S]` is not decoration: without it `_sync_cookies_to_vault`
    returns on an empty `_etld1s` and the window saves nothing at all."""
    ctx = _StubContext()

    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )

    assert ctx.routes and ctx.routes[0][0] == "**/*"
    assert ab._etld1s == {TEST_DOMAIN}


def test_a_login_window_with_no_scope_says_so_instead_of_saving_nothing(
    vault_home, caplog,
):
    """Trap 3: `if not cookies or not self._etld1s: return` is a silent
    no-op. On the login-window path silence means the human logged in and
    the vault stayed empty with nothing in the log to say why."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[], headed=True, login_window=True)
    with caplog.at_level(logging.ERROR):
        scoped = ab._scope_cookies_by_etld1(_pw_cookies())

    assert scoped == {}
    assert any("no eTLD+1 scope" in r.getMessage() for r in caplog.records)


# ── the write: cookies are data, approval is a decision ─────────────────


def test_closing_a_login_window_writes_nothing_at_all(vault_home, monkeypatch):
    """Cookies are data, the approval is a decision — and for a login window
    neither is written by the browser. What the window collected sits in
    memory until a person says yes, so a window that reaches its close with
    nobody answering has touched no jar."""
    from dpc_client_core import web_auth

    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ab.close()

    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"] is False
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert [c["name"] for c in ab._login_pending[TEST_DOMAIN]] == ["s"], (
        "and yet the window did collect it — into memory"
    )


def test_no_browser_path_writes_an_approval_row(vault_home, monkeypatch):
    """`login_approved` is written where the person answered, and nowhere
    the browser can reach on its own."""
    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ab.capture_login_cookies()
    ab.close()

    rows = [e for e in _read_audit(vault_home, "agent_a")
            if e.get("action") == "login_approved"]
    assert rows == []


def test_an_ordinary_navigate_and_close_creates_no_approval(vault_home, monkeypatch):
    """The measured defect, pinned. The cookie writeback runs after every
    navigate and again on close; before this change each of those stamped
    the jar and the gate read the stamp as a human's approval."""
    from dpc_client_core import web_auth

    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, _seen = _open_browser(monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True)

    ab._persist_session_cookies()
    ab.close()

    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"], (
        "cookies still persist — only the approval is withheld"
    )
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert web_auth.is_approved("agent_a", TEST_DOMAIN) is False


def test_an_ordinary_close_does_not_renew_an_existing_approval(vault_home, monkeypatch):
    """Refreshed cookie bytes are neither a re-approval nor a revocation:
    the approval timestamp must be the human's, not the browser's."""
    from dpc_client_core import web_auth

    web_auth.save_cookies(
        "agent_a", TEST_DOMAIN, _fresh_cookies(),
        approved_via=web_auth.APPROVAL_VIA_LOGIN_WINDOW,
    )
    granted = web_auth.get_approval("agent_a", TEST_DOMAIN)

    # `_now_iso` has second resolution, and the whole test runs inside one
    # second — so a restamp would have written the identical timestamp and
    # gone unseen. Move the clock instead of hoping it moves.
    monkeypatch.setattr(web_auth, "_now_iso", lambda: "2099-01-01T00:00:00Z")

    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, _seen = _open_browser(monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True)
    ab._persist_session_cookies()
    ab.close()

    after = web_auth.get_approval("agent_a", TEST_DOMAIN)
    assert after == granted, "the browser must not restamp the human's decision"
    assert after["at"] != "2099-01-01T00:00:00Z"
    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["authenticated_at"] == (
        "2099-01-01T00:00:00Z"
    ), "the cookie bytes DID get refreshed — that is the field for it"


def test_the_js_fallback_fetch_still_writes_nothing_to_the_vault(vault_home, monkeypatch):
    """`AuthBrowser(domains=[], headed=False, anonymous=True)` is the other
    consumer of the clean-start behaviour. Had `anonymous` been given the
    new meaning "may write to the vault", this fetch would have become a
    vault writer."""
    from dpc_client_core import web_auth

    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, seen = _open_browser(
        monkeypatch, ctx, domains=[], headed=False, anonymous=True,
    )
    ab._persist_session_cookies()
    ab.close()

    assert "storage_state" not in seen[0]
    assert ctx.added == []
    assert web_auth.list_domains("agent_a") == []
    assert not (vault_home / "agents" / "agent_a" / "browser_state.json").exists()


def test_a_login_window_leaves_the_shared_browser_state_alone(vault_home, monkeypatch):
    """It knows exactly one site. Writing browser_state.json from here would
    overwrite the interactive session's state with that one site's."""
    state = vault_home / "agents" / "agent_a" / "browser_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"cookies": [{"name": "keep"}], "origins": []}),
                     encoding="utf-8")

    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ab.close()

    assert json.loads(state.read_text(encoding="utf-8"))["cookies"] == [{"name": "keep"}]


def test_a_captured_login_is_not_rewritten_on_every_poll(vault_home, monkeypatch):
    """The window is polled every couple of seconds because the human
    closing it kills the context before close() can read it. An unchanged
    cookie set must not cost a vault round trip each tick."""
    ctx = _StubContext(cookies_payload=_pw_cookies())
    ab, _seen = _open_browser(
        monkeypatch, ctx, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )

    assert ab.capture_login_cookies() == 1
    assert ab.capture_login_cookies() == 0
    ctx.cookies_payload = _pw_cookies() + [
        {"name": "auth", "value": "t", "domain": f".{TEST_DOMAIN}", "path": "/"},
    ]
    assert ab.capture_login_cookies() == 2


# ── nobody at the screen ────────────────────────────────────────────────


def test_a_login_window_is_refused_when_no_ui_client_is_connected(vault_home):
    """A window nobody is looking at cannot collect a password."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    class _NoUiApi:
        has_clients = False

        async def broadcast_event(self, name, payload):  # pragma: no cover
            raise AssertionError("nothing to broadcast to")

    out = asyncio.run(browser_mod.open_login_window(
        _Ctx(vault_home, local_api=_NoUiApi()), domain=TEST_DOMAIN,
    ))

    assert "no UI client is connected" in out
    assert browser_mod.get_login_windows() == {}
    rows = _read_audit(vault_home, "agent_a")
    assert rows[-1]["status"] == "login_window_denied:headless_no_ui"


def test_the_no_ui_refusal_opens_no_browser(vault_home, monkeypatch):
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    monkeypatch.setattr(
        browser_mod, "AuthBrowser",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("a browser was constructed after the refusal")
        ),
    )

    class _NoUiApi:
        has_clients = False

    asyncio.run(browser_mod.open_login_window(
        _Ctx(vault_home, local_api=_NoUiApi()), domain=TEST_DOMAIN,
    ))


def test_a_public_suffix_cannot_be_logged_into(vault_home):
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    out = asyncio.run(browser_mod.open_login_window(
        _Ctx(vault_home, local_api=None), domain="com",
    ))

    assert "not a registrable domain" in out
    assert _read_audit(vault_home, "agent_a")[-1]["status"] == (
        "login_window_denied:not_a_domain"
    )


# ── the tool itself ─────────────────────────────────────────────────────


def test_open_login_window_is_registered_and_opt_in():
    """S148: `default_enabled` is explicit, and this tool fails closed —
    it puts a password prompt on the user's screen and is the only act that
    can create a web-auth approval."""
    from dpc_client_core.dpc_agent.tools.browser import get_tools

    entry = next(t for t in get_tools() if t.name == "open_login_window")
    assert entry.default_enabled is False
    assert entry.schema["parameters"]["required"] == ["domain"]


def test_the_browse_page_description_names_no_deleted_ui():
    """The web-auth UI was deleted in c2cfab07; a tool description is what
    the model plans against."""
    from dpc_client_core.dpc_agent.tools.browser import get_tools

    entry = next(t for t in get_tools() if t.name == "browse_page")
    text = entry.schema["description"]
    assert "web-auth UI" not in text
    assert "open_login_window" in text


def test_the_open_login_window_description_says_what_the_tool_does():
    """Two sentences of it were false, and one of them taught the model the
    misconception this mechanism was built to remove: that closing the
    window records the approval. The other claimed no other site is
    reachable from a window that is deliberately ungated, so a sign-in can
    reach its identity provider. A description is read every turn."""
    from dpc_client_core.dpc_agent.tools.browser import get_tools

    entry = next(t for t in get_tools() if t.name == "open_login_window")
    text = entry.schema["description"]

    assert "no other site is reachable" not in text
    assert "ungated" in text or "any host" in text
    assert "Closing the window saves those cookies" not in text
    assert "only their yes" in text
    assert "writes nothing" in text


def test_list_auth_domains_no_longer_speaks_of_a_whitelist():
    """The whitelist was killed on 2026-09-10; two places went on naming it."""
    from dpc_client_core.dpc_agent.tools import web_auth_tools

    entry = web_auth_tools.get_tools()[0]
    assert "whitelist" not in entry.schema["description"].lower()
    assert "whitelist" not in (web_auth_tools.__doc__ or "").lower()


def test_list_auth_domains_reports_approval_not_just_cookies(vault_home):
    """A listing that says "authenticated" for a jar the gate refuses is the
    same mirror-not-a-gate error one layer up."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    web_auth.save_cookies("agent_a", "unapproved.example", _fresh_cookies("unapproved.example"))
    web_auth.save_cookies(
        "agent_a", "approved.example", _fresh_cookies("approved.example"),
        approved_via=web_auth.APPROVAL_VIA_LOGIN_WINDOW,
    )

    out = asyncio.run(list_auth_domains(_Ctx(vault_home)))

    unapproved = next(l for l in out.splitlines() if "unapproved.example" in l)
    approved = next(l for l in out.splitlines()
                    if "approved.example" in l and "unapproved" not in l)
    assert "NOT approved" in unapproved
    assert "open_login_window" in unapproved
    assert "NOT approved" not in approved
