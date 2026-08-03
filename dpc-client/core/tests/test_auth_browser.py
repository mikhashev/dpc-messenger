"""Tests for ADR-028 T4: AuthBrowser + browse_page use_auth path.

The Camoufox-backed methods (`_open`, `goto`, `get_page_content`,
`close`) are NOT exercised here — they require Camoufox + a real
browser binary which is an optional extra. Coverage here is on the
guard logic that surrounds the browser:

  - AuthRequiredError / AuthExpiredError raised before any browser launch
  - Restricted public surface (only goto / get_page_content / close /
    domain — no click / fill / evaluate)
  - Domain-leak prevention (_domain_matches)
  - Cookie format conversion (snake_case → Playwright camelCase)
  - browse_page use_auth path returns re-login prompt when vault empty
  - Anonymous browse_page path unchanged (regression)
"""
from __future__ import annotations

from .conftest import TEST_DOMAIN, TEST_DOMAIN_WWW, TEST_DOMAIN_URL
import asyncio
import json
import time
import types
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────
# Fixtures (re-use the keyring + DPC_HOME isolation from test_web_auth)
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DPC_HOME", str(tmp_path))

    import keyring
    from keyring import backend

    class _MemKeyring(backend.KeyringBackend):
        priority = 1  # type: ignore[assignment]

        def __init__(self):
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    previous = keyring.get_keyring()
    keyring.set_keyring(_MemKeyring())
    yield tmp_path
    keyring.set_keyring(previous)


@pytest.fixture
def fresh_cookies():
    future = int(time.time()) + 3600
    return [
        {
            "name": "session_id",
            "value": "abc",
            "domain": f".{TEST_DOMAIN}",
            "path": "/",
            "expires": future,
            "secure": True,
            "httponly": True,
            "samesite": "Lax",
        }
    ]


@pytest.fixture
def expired_cookies():
    past = int(time.time()) - 3600
    return [
        {
            "name": "session_id",
            "value": "abc",
            "domain": f".{TEST_DOMAIN}",
            "path": "/",
            "expires": past,
            "secure": True,
            "httponly": True,
            "samesite": "Lax",
        }
    ]


# ─────────────────────────────────────────────────────────────
# AuthRequiredError / AuthExpiredError raised before browser open
# ─────────────────────────────────────────────────────────────


def test_auth_required_raised_on_load(vault_home):
    """ADR-029: cookies load lazily in `_load_all_cookies` (called from
    `_open()`), not at construction. AuthRequiredError fires when the
    browser is opened for a domain with no stored cookies."""
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        AuthRequiredError,
    )

    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    with pytest.raises(AuthRequiredError) as exc:
        ab._load_all_cookies()
    assert f"{TEST_DOMAIN}" in str(exc.value)
    assert "re-login" in str(exc.value).lower()


def test_auth_expired_raised_on_load(vault_home, expired_cookies):
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        AuthExpiredError,
    )

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", expired_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    with pytest.raises(AuthExpiredError) as exc:
        ab._load_all_cookies()
    assert f"{TEST_DOMAIN}" in str(exc.value)
    assert "expired" in str(exc.value).lower()


def test_construction_is_lazy(vault_home):
    """ADR-029: AuthBrowser construction does no I/O — no keyring read,
    no Camoufox import, no browser launch. Verifies the lazy contract
    that makes per-agent session registry safe to instantiate."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    assert ab.domain == f"{TEST_DOMAIN}"
    assert ab.domains == [f"{TEST_DOMAIN}"]
    assert ab._page is None
    assert ab._cookies_loaded is False


# ─────────────────────────────────────────────────────────────
# Restricted public surface — no interactive methods leaked
# ─────────────────────────────────────────────────────────────


def test_authbrowser_public_surface(vault_home):
    """ADR-029 Task 002 introduces interactive methods (scroll, click,
    fill, etc.) — the ADR-028 read-only restriction is intentionally
    lifted for the headed-session flow. Track the new contract here so
    future accidental removals or unintended additions are caught."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    public = {n for n in dir(ab) if not n.startswith("_")}
    # Allowed surface — read methods + ADR-029 interactive methods
    expected = {
        "goto", "navigate", "get_page_html", "get_page_content",
        "close", "domain", "domains", "headed", "start",
        "scroll", "click", "fill", "screenshot", "wait_for",
        "extract", "switch_tab", "wait_for_popup",
    }
    assert expected.issubset(public), f"missing methods: {expected - public}"
    # Still forbidden — direct Playwright handles (these are the leaky
    # primitives that would bypass our domain gate and tool-level audit)
    forbidden = {"page", "context", "browser", "request"}
    leaked = forbidden & public
    assert not leaked, f"forbidden methods exposed: {leaked}"


# ─────────────────────────────────────────────────────────────
# _domain_matches — eTLD+1 leak prevention
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url,etld1,expected", [
    # Same domain
    (f"https://{TEST_DOMAIN}/path", f"{TEST_DOMAIN}", True),
    # Subdomain
    (f"https://www.{TEST_DOMAIN}/path", f"{TEST_DOMAIN}", True),
    (f"https://login.{TEST_DOMAIN}/oauth", f"{TEST_DOMAIN}", True),
    (f"https://api.{TEST_DOMAIN}/v2/orders", f"{TEST_DOMAIN}", True),
    # Different TLD
    ("https://example.org/path", f"{TEST_DOMAIN}", False),
    # Different domain
    ("https://example.net/", f"{TEST_DOMAIN}", False),
    # Adversarial — auth domain in path/query, not host
    (f"https://attacker.example/?ref={TEST_DOMAIN}", f"{TEST_DOMAIN}", False),
    (f"https://attacker.example/{TEST_DOMAIN}/page", f"{TEST_DOMAIN}", False),
    # Adversarial — auth domain as suffix-LIKE but not subdomain
    (f"https://not{TEST_DOMAIN}/", f"{TEST_DOMAIN}", False),
    (f"https://fake-{TEST_DOMAIN}/", f"{TEST_DOMAIN}", False),
    # Malformed
    ("not-a-url", f"{TEST_DOMAIN}", False),
    ("", f"{TEST_DOMAIN}", False),
])
def test_domain_matches(url, etld1, expected):
    from dpc_client_core.dpc_agent.tools.browser import _domain_matches

    assert _domain_matches(url, etld1) is expected


def test_goto_rejects_off_domain_url(vault_home, fresh_cookies):
    """goto() must raise ValueError BEFORE making any network request
    when URL is outside the auth domain. Verified by calling goto on an
    AuthBrowser that hasn't been opened — ValueError fires from the
    domain check; if it leaks past, RuntimeError would fire from the
    not-opened guard instead. We assert ValueError specifically."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    # Inject a stub _page so the not-opened guard does NOT trip first
    ab._page = object()
    with pytest.raises(ValueError) as exc:
        ab.goto("https://example.net/search")
    assert "outside auth domain" in str(exc.value)


# ─────────────────────────────────────────────────────────────
# ADR-029 Task 002 — multi-domain init + session registry + headed
# ─────────────────────────────────────────────────────────────


def test_multi_domain_constructor(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[f"{TEST_DOMAIN}", "example.net"], headed=True
    )
    assert set(ab.domains) == {f"{TEST_DOMAIN}", "example.net"}
    assert ab.headed is True
    assert ab.domain == f"{TEST_DOMAIN}"  # back-compat scalar = first


def test_constructor_rejects_both_domain_and_domains(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    with pytest.raises(ValueError, match="domains.*or.*domain"):
        AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}", domains=["example.net"])


def test_multi_domain_check_allows_any_etld1(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}", "example.net"])
    ab._page = object()
    # Both allowed, no exception
    ab._check_domain(f"https://{TEST_DOMAIN}/orders")
    ab._check_domain("https://www.example.net/search")
    # Off-domain still rejected
    with pytest.raises(ValueError, match="outside auth domains"):
        ab._check_domain("https://attacker.example/")


def test_session_registry_reuse(vault_home, fresh_cookies):
    """Ark D2 duplicate-open guard: second _get_or_create_session call
    for the same agent reuses the live session instead of creating a
    fresh Camoufox subprocess."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        _active_browser_sessions,
        get_active_browser_sessions,
    )

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    # Stub a live session so the guard sees `_page is not None`
    stub = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    stub._page = object()  # masquerade as opened without launching Camoufox
    _active_browser_sessions["agent_a"] = stub
    try:
        assert get_active_browser_sessions()["agent_a"] is stub
        # Cleanup also wipes the dict entry
        stub.close()
        assert "agent_a" not in _active_browser_sessions
    finally:
        _active_browser_sessions.pop("agent_a", None)


def test_screenshot_save_to_returns_path(vault_home, tmp_path):
    """Q1 escape hatch: passing save_to writes to disk and returns the
    path (string), not bytes. Verified without launching Camoufox by
    stubbing the page object."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    target = tmp_path / "shot.png"
    calls: list[dict] = []

    class StubPage:
        def screenshot(self, **kwargs):
            calls.append(kwargs)
            return b"PNG-bytes"

    ab._page = StubPage()
    result = ab.screenshot(full_page=True, save_to=str(target))
    assert result == str(target)
    assert calls[0]["path"] == str(target)


# ─────────────────────────────────────────────────────────────
# ADR-029 Task 003 — Playwright route handler (domain restriction)
# ─────────────────────────────────────────────────────────────


class _FakeRoute:
    """Minimal Playwright Route stub — exposes `request.url`, records
    whether continue_ or abort was called. Mirrors only the surface our
    `_domain_route_gate` touches."""

    def __init__(self, url: str):
        self.request = types.SimpleNamespace(url=url)
        self.continued = False
        self.aborted = False

    def continue_(self):
        self.continued = True

    def abort(self):
        self.aborted = True


class _FakeContext:
    """Captures the route handler installed by `_install_domain_route_handler`
    so we can invoke it with fake routes."""

    def __init__(self):
        self.registered: list[tuple[str, object]] = []

    def route(self, pattern: str, handler):
        self.registered.append((pattern, handler))


def test_install_route_handler_registers_catch_all(vault_home):
    """`_install_domain_route_handler` must register a `**/*` route on
    the context so EVERY request (page + XHR + redirects) goes through
    the gate."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    ab._context = _FakeContext()
    ab._install_domain_route_handler()
    assert len(ab._context.registered) == 1
    pattern, _handler = ab._context.registered[0]
    assert pattern == "**/*"


def test_route_gate_allows_in_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    route = _FakeRoute(f"https://{TEST_DOMAIN}/my/orders")
    ab._domain_route_gate(route)
    assert route.continued is True
    assert route.aborted is False
    assert ab._domain_blocks == 0


def test_route_gate_allows_subdomain(vault_home):
    """Subdomain CDN of an allowed eTLD+1 must pass — page-asset case
    from spec (cdn.shop.example.com when shop.example.com whitelisted)."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    route = _FakeRoute(f"https://cdn.{TEST_DOMAIN}/static/lib.js")
    ab._domain_route_gate(route)
    assert route.continued is True
    assert route.aborted is False


def test_route_gate_blocks_off_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    route = _FakeRoute("https://googletagmanager.com/gtm.js?id=GTM-XXX")
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False
    assert ab._domain_blocks == 1


def test_route_gate_blocks_lookalike(vault_home):
    """Adversarial host that contains the whitelisted eTLD+1 as a
    substring (not<TEST_DOMAIN>, fake-<TEST_DOMAIN>) must be aborted — eTLD+1
    resolution, not substring match."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    for hostile in (
        f"https://not{TEST_DOMAIN}/payload",
        f"https://fake-{TEST_DOMAIN}/payload",
        f"https://attacker.example/?ref={TEST_DOMAIN}",
    ):
        route = _FakeRoute(hostile)
        ab._domain_route_gate(route)
        assert route.aborted is True, hostile
        assert route.continued is False, hostile
    assert ab._domain_blocks == 3


def test_route_gate_multi_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}", "example.net"])
    for ok in (
        f"https://{TEST_DOMAIN}/orders",
        "https://www.example.net/search",
        f"https://api.{TEST_DOMAIN}/v2/x",
    ):
        route = _FakeRoute(ok)
        ab._domain_route_gate(route)
        assert route.continued is True, ok
    route = _FakeRoute("https://example.org/search")
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert ab._domain_blocks == 1


def test_route_gate_fail_closed_when_empty(vault_home):
    """Empty whitelist → every request aborted (no domains authorized)."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[])
    route = _FakeRoute(f"https://{TEST_DOMAIN}/my")
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False
    assert ab._domain_blocks == 1


def test_route_gate_allows_non_http_schemes(vault_home):
    """data:, about:, blob: URIs do not trigger external network
    requests — short-circuit ALLOW per spec."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    for uri in (
        "data:text/html,<html><body>x</body></html>",
        "about:blank",
        f"blob:https://{TEST_DOMAIN}/abc-123",
    ):
        route = _FakeRoute(uri)
        ab._domain_route_gate(route)
        assert route.continued is True, uri
        assert route.aborted is False, uri
    assert ab._domain_blocks == 0


def test_route_gate_resilient_to_broken_request(vault_home):
    """Defensive: if Playwright surfaces an unexpected Route shape
    where .request.url raises, the gate aborts (fail-closed) instead
    of letting the request through."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    class _BrokenRoute:
        def __init__(self):
            self.aborted = False
            self.continued = False

        @property
        def request(self):
            raise RuntimeError("broken playwright route")

        def abort(self):
            self.aborted = True

        def continue_(self):
            self.continued = True

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    route = _BrokenRoute()
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False


# ─────────────────────────────────────────────────────────────
# ADR-029 Task 004 — storage_state + vault hybrid persistence
# ─────────────────────────────────────────────────────────────


class _FakeStateContext:
    """Mock of Playwright BrowserContext that captures storage_state()
    + add_cookies() calls and writes JSON when storage_state(path=...)
    is invoked. Combined with _FakeRoute/_FakeContext above this gives
    us enough surface to drive AuthBrowser through _open/close without
    a real Camoufox binary."""

    def __init__(self, on_storage_state=None, cookies_payload=None):
        self.added: list[list[dict]] = []
        self.routes: list[tuple[str, object]] = []
        self.pages: list[object] = []
        self._on_storage_state = on_storage_state
        self.cookies_payload: list[dict] = list(cookies_payload or [])
        self.closed = False
        self.storage_state_calls: list[str] = []
        self.cookies_calls = 0

    def add_cookies(self, cookies: list[dict]) -> None:
        self.added.append(list(cookies))

    def route(self, pattern: str, handler) -> None:
        self.routes.append((pattern, handler))

    def new_page(self):
        page = object()
        self.pages.append(page)
        return page

    def storage_state(self, path: str | None = None) -> dict | None:
        """Kept so a test can assert it is NOT called.

        Saving via storage_state() collects localStorage, and Firefox reads
        that by opening a window on each origin — measured at one extra
        visible window per origin, appearing and vanishing within a second,
        after every navigate and at close. The save path uses cookies()
        instead; the origins it stopped collecting were discarded on load
        anyway."""
        self.storage_state_calls.append(path or "<no-path>")
        if self._on_storage_state is not None:
            return self._on_storage_state(path)
        return None

    def cookies(self) -> list[dict]:
        self.cookies_calls = getattr(self, "cookies_calls", 0) + 1
        return list(self.cookies_payload)

    def close(self) -> None:
        self.closed = True


def test_from_playwright_cookies_drops_session_marker(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import _from_playwright_cookies

    src = [
        {"name": "lang", "value": "en", "domain": ".example.net", "expires": -1},
        {"name": "session", "value": "x", "domain": ".example.net"},
        {"name": "valid", "value": "v", "domain": ".example.net", "expires": 1735689600},
    ]
    out = _from_playwright_cookies(src)
    assert "expires" not in out[0]
    assert "expires" not in out[1]
    assert out[2]["expires"] == 1735689600


def test_from_playwright_cookies_roundtrip(vault_home):
    """camelCase Playwright cookie → snake_case vault cookie →
    re-converted back to camelCase preserves all fields."""
    from dpc_client_core.dpc_agent.tools.browser import (
        _from_playwright_cookies,
        _to_playwright_cookies,
    )

    src = [
        {
            "name": "session_id", "value": "abc",
            "domain": f".{TEST_DOMAIN}", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
            "expires": 1735689600,
        },
    ]
    snake = _from_playwright_cookies(src)
    assert snake[0]["httponly"] is True
    assert snake[0]["samesite"] == "Lax"
    assert snake[0]["expires"] == 1735689600
    assert "httpOnly" not in snake[0]
    # And back — should match Playwright shape modulo defaulting
    rt = _to_playwright_cookies(snake)
    assert rt[0]["httpOnly"] is True
    assert rt[0]["sameSite"] == "Lax"
    assert rt[0]["expires"] == 1735689600


def test_state_path_uses_dpc_home(vault_home):
    """`_state_path` resolves to ~/.dpc/agents/<id>/browser_state.json
    under the DPC_HOME env override (set by vault_home fixture)."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    state = ab._state_path()
    expected = vault_home / "agents" / "agent_a" / "browser_state.json"
    assert state == expected


def test_inject_vault_cookies_calls_add_cookies(vault_home, fresh_cookies):
    """`_inject_vault_cookies` reads vault via _load_all_cookies and
    pushes the camelCase-converted cookies into the active context."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    ab._context = _FakeStateContext()
    ab._inject_vault_cookies()
    assert len(ab._context.added) == 1
    assert ab._context.added[0][0]["name"] == "session_id"
    assert ab._context.added[0][0]["httpOnly"] is True  # camelCase converted


def test_sync_cookies_to_vault_groups_and_filters(vault_home):
    """Group Playwright cookies by eTLD+1, save each group, drop cookies
    whose domain is outside the session whitelist."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    state_cookies = [
        {
            "name": "a", "value": "1", "domain": f".{TEST_DOMAIN}", "path": "/",
            "secure": True, "httpOnly": False, "sameSite": "Lax",
            "expires": 1735689600,
        },
        {
            "name": "b", "value": "2", "domain": f"www.{TEST_DOMAIN}", "path": "/",
            "secure": True, "httpOnly": True, "sameSite": "Lax",
            "expires": 1735689600,
        },
        {
            # Outside whitelist — must NOT land in vault even if route
            # handler missed it.
            "name": "leak", "value": "3", "domain": ".example.org", "path": "/",
            "secure": True, "httpOnly": True,
        },
    ]
    ab._sync_cookies_to_vault(state_cookies)
    saved = web_auth.load_cookies("agent_a", f"{TEST_DOMAIN}")
    assert saved is not None
    names = sorted(c["name"] for c in saved)
    assert names == ["a", "b"]
    # example.org cookie not saved anywhere
    assert web_auth.load_cookies("agent_a", "example.org") is None


def test_sync_cookies_to_vault_empty_input_no_op(vault_home):
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    ab._sync_cookies_to_vault([])  # must not raise
    assert web_auth.load_cookies("agent_a", f"{TEST_DOMAIN}") is None


def test_save_storage_state_writes_atomically_and_syncs_vault(vault_home):
    """`_save_storage_state` writes to a `.tmp` sibling then os.replaces,
    final file exists, vault has the synced cookies."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])

    state_dict = {
        "cookies": [
            {
                "name": "s", "value": "v", "domain": f".{TEST_DOMAIN}",
                "path": "/", "secure": True, "httpOnly": True,
                "sameSite": "Lax", "expires": 1735689600,
            },
        ],
        "origins": [],
    }

    def _write_state(path: str):
        Path(path).write_text(json.dumps(state_dict), encoding="utf-8")
        return state_dict

    ab._context = _FakeStateContext(
        on_storage_state=_write_state,
        cookies_payload=state_dict["cookies"],
    )
    ab._save_storage_state()

    state_path = ab._state_path()
    assert state_path.exists()
    # tmp was os.replaced — no .tmp leftover
    assert not state_path.with_suffix(".json.tmp").exists()
    saved = web_auth.load_cookies("agent_a", f"{TEST_DOMAIN}")
    assert saved is not None
    assert saved[0]["name"] == "s"
    assert saved[0]["httponly"] is True  # snake_case vault format


def test_save_storage_state_uses_return_value_not_disk_read(vault_home, monkeypatch):
    """ADR-029 Task 004 follow-up — `_save_storage_state` consumes the
    dict returned by `storage_state(path=...)` instead of reading the
    file back. Verified by stubbing read_text to raise: the save still
    completes and vault sync runs from the in-memory dict."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as mod
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    state_dict = {
        "cookies": [
            {
                "name": "s", "value": "v", "domain": f".{TEST_DOMAIN}",
                "path": "/", "secure": True, "httpOnly": True,
                "expires": 1735689600,
            },
        ],
        "origins": [],
    }

    def _write_state(path: str):
        Path(path).write_text("not-used-because-we-return-dict", encoding="utf-8")
        return state_dict

    # Make Path.read_text raise so a regression (reading the file back)
    # would fail loudly instead of silently passing on disk content.
    original_read = Path.read_text

    def _no_read(self, *args, **kwargs):
        raise AssertionError(f"unexpected read_text on {self}")

    monkeypatch.setattr(Path, "read_text", _no_read)
    try:
        ab._context = _FakeStateContext(
        on_storage_state=_write_state,
        cookies_payload=state_dict["cookies"],
    )
        ab._save_storage_state()
    finally:
        monkeypatch.setattr(Path, "read_text", original_read)

    saved = web_auth.load_cookies("agent_a", f"{TEST_DOMAIN}")
    assert saved is not None and saved[0]["name"] == "s"


def _patch_browser_os(monkeypatch, name: str, chmod_handler):
    """Replace the `os` reference inside the browser module with a
    SimpleNamespace fake so `os.name` / `os.chmod` patches stay scoped
    to browser.py and don't bleed into web_auth.py (where the real
    `Path.home()` would crash if we forced os.name='posix' on a
    Windows host — eager default in `_vault_path` triggers PosixPath
    construction)."""
    import os as real_os
    from dpc_client_core.dpc_agent.tools import browser as mod

    fake_os = types.SimpleNamespace(
        name=name,
        chmod=chmod_handler,
        replace=real_os.replace,
        environ=real_os.environ,
    )
    monkeypatch.setattr(mod, "os", fake_os)


def test_save_storage_state_chmod_on_posix(vault_home, monkeypatch):
    """ADR-029 Task 004 follow-up — restrict `browser_state.json` to
    owner (0o600) on POSIX so other users on the machine can't read
    plaintext session cookies."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    state_dict = {"cookies": [], "origins": []}

    def _write_state(path: str):
        Path(path).write_text(json.dumps(state_dict), encoding="utf-8")
        return state_dict

    chmod_calls: list[tuple[str, int]] = []

    def _capture_chmod(path, mode):
        chmod_calls.append((str(path), mode))

    _patch_browser_os(monkeypatch, "posix", _capture_chmod)

    ab._context = _FakeStateContext(
        on_storage_state=_write_state,
        cookies_payload=state_dict["cookies"],
    )
    ab._save_storage_state()

    state_path = ab._state_path()
    assert chmod_calls == [(str(state_path), 0o600)]


def test_save_storage_state_no_chmod_on_non_posix(vault_home, monkeypatch):
    """`os.chmod(_, 0o600)` is POSIX-only — must not run on Windows
    (where NTFS ACLs inherit from the parent dir)."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])

    def _write_state(path: str):
        Path(path).write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
        return {"cookies": [], "origins": []}

    chmod_called: list = []

    def _record_chmod(path, mode):
        chmod_called.append((path, mode))

    _patch_browser_os(monkeypatch, "nt", _record_chmod)

    ab._context = _FakeStateContext(
        on_storage_state=_write_state,
        cookies_payload=[],
    )
    ab._save_storage_state()

    assert chmod_called == []


def test_save_storage_state_swallows_chmod_oserror(vault_home, monkeypatch, caplog):
    """A failing `os.chmod` (e.g. filesystem doesn't support it — FAT32
    USB drive) must not break the save flow. Vault sync still happens,
    warning is logged."""
    import logging as _logging
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    state_dict = {
        "cookies": [
            {"name": "x", "value": "y", "domain": f".{TEST_DOMAIN}", "path": "/",
             "secure": False, "httpOnly": False, "expires": 1735689600},
        ],
        "origins": [],
    }

    def _write_state(path: str):
        Path(path).write_text(json.dumps(state_dict), encoding="utf-8")
        return state_dict

    def _raise_chmod(path, mode):
        raise OSError("filesystem does not support chmod")

    _patch_browser_os(monkeypatch, "posix", _raise_chmod)

    ab._context = _FakeStateContext(
        on_storage_state=_write_state,
        cookies_payload=state_dict["cookies"],
    )
    with caplog.at_level(_logging.WARNING):
        ab._save_storage_state()

    saved = web_auth.load_cookies("agent_a", f"{TEST_DOMAIN}")
    assert saved is not None and saved[0]["name"] == "x"
    assert any(
        "storage_state chmod failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_save_storage_state_swallows_errors(vault_home, caplog):
    """`_save_storage_state` must not raise on context errors — close()
    relies on this so subprocess cleanup runs to completion."""
    import logging as _logging
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])

    class _BrokenContext:
        def storage_state(self, path=None):
            raise RuntimeError("context already closed")

    ab._context = _BrokenContext()
    with caplog.at_level(_logging.WARNING):
        ab._save_storage_state()  # must not raise
    assert any(
        "storage_state save failed" in rec.getMessage()
        for rec in caplog.records
    )


def test_save_storage_state_no_op_when_context_none(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    assert ab._context is None
    ab._save_storage_state()  # must not raise
    assert not ab._state_path().exists()


def test_close_triggers_save_when_context_live(vault_home):
    """close() must call _save_storage_state before tearing down so
    Playwright storage_state() has a live context to read from."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    saved: list[bool] = []

    def _record_save():
        saved.append(ab._context is not None)

    # Stub _save_storage_state to record when it was called relative to cm
    original = ab._save_storage_state
    ab._save_storage_state = _record_save
    ab._context = _FakeStateContext()

    # No Camoufox cm — close should still call save (early branch)
    ab.close()
    assert saved == [True]


def test_save_storage_state_skips_quietly_when_disconnected(vault_home, caplog):
    """When the browser already disconnected (e.g. user closed the window),
    `_save_storage_state` must skip without raising and WITHOUT a WARNING —
    the dead context is unreadable, so the failure is expected, not a fault."""
    import logging as _logging
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])

    class _BrokenContext:
        def storage_state(self, path=None):
            raise RuntimeError("Target page, context or browser has been closed")

    ab._context = _BrokenContext()
    ab._disconnected = True
    with caplog.at_level(_logging.DEBUG):
        ab._save_storage_state()  # must not raise
    assert not any(rec.levelno >= _logging.WARNING for rec in caplog.records)


def test_close_runs_cm_exit_even_when_disconnected(vault_home):
    """close() must tear down the Camoufox cm (`__exit__`) even after a
    disconnect, so the driver subprocess + its OS pipe are released. Skipping
    it orphaned the pipe → Windows IocpProactor spin at shutdown."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    exited: list[bool] = []

    class _FakeCM:
        def __exit__(self, *args):
            exited.append(True)
            return False

    ab._cm = _FakeCM()
    ab._disconnected = True  # browser already detached

    ab.close()
    assert exited == [True]  # subprocess teardown ran despite disconnect
    assert ab._cm is None


def test_open_uses_storage_state_when_file_valid(vault_home, monkeypatch):
    """When `browser_state.json` exists + parses, _open() passes
    `storage_state=<path>` to new_context() and skips the vault
    injection path."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    # Pre-create the state file
    state_dir = vault_home / "agents" / "agent_a"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "browser_state.json"
    state_path.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    new_context_kwargs: list[dict] = []
    injected: list[int] = []

    class _StubBrowser:
        def new_context(self, **kwargs):
            new_context_kwargs.append(dict(kwargs))
            return _FakeStateContext()

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *args):
            return False

    # Patch Camoufox to return our stub instead of launching a real browser
    import dpc_client_core.dpc_agent.tools.browser as mod
    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(),
        raising=False,
    )
    # Patch _inject_vault_cookies so we can detect if it was called
    original = AuthBrowser._inject_vault_cookies
    AuthBrowser._inject_vault_cookies = lambda self, **kw: injected.append(1)
    try:
        ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
        ab._open()
    finally:
        AuthBrowser._inject_vault_cookies = original
        # Best-effort cleanup of process-wide registry the stub joined
        from dpc_client_core.dpc_agent.tools.browser import _active_camoufox_browsers
        _active_camoufox_browsers.discard(ab)

    assert len(new_context_kwargs) == 1
    assert new_context_kwargs[0].get("storage_state") == str(state_path)
    # Vault is canonical: even with a valid storage_state, vault cookies are
    # always overlaid on top (browser.py — "always overlay vault cookies"),
    # so _inject_vault_cookies still runs. storage_state is a starting point
    # for localStorage/sessionStorage, not an either/or with the vault.
    assert injected == [1]


def test_open_falls_back_to_vault_when_state_missing(vault_home, monkeypatch, fresh_cookies):
    """No state file → new_context() called with no storage_state kwarg,
    vault injection path is taken."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)

    new_context_kwargs: list[dict] = []
    injected: list[int] = []

    class _StubBrowser:
        def new_context(self, **kwargs):
            new_context_kwargs.append(dict(kwargs))
            return _FakeStateContext()

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(),
        raising=False,
    )
    original = AuthBrowser._inject_vault_cookies
    AuthBrowser._inject_vault_cookies = lambda self, **kw: injected.append(1)
    try:
        ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
        ab._open()
    finally:
        AuthBrowser._inject_vault_cookies = original
        from dpc_client_core.dpc_agent.tools.browser import _active_camoufox_browsers
        _active_camoufox_browsers.discard(ab)

    assert len(new_context_kwargs) == 1
    assert "storage_state" not in new_context_kwargs[0]
    assert injected == [1]


def test_open_falls_back_to_vault_when_state_corrupt(vault_home, monkeypatch, fresh_cookies, caplog):
    """Corrupt JSON in browser_state.json → warning logged, fallback to
    vault, next close will overwrite the file."""
    import logging as _logging
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)

    state_dir = vault_home / "agents" / "agent_a"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "browser_state.json").write_text("not-valid-json{", encoding="utf-8")

    new_context_kwargs: list[dict] = []
    injected: list[int] = []

    class _StubBrowser:
        def new_context(self, **kwargs):
            new_context_kwargs.append(dict(kwargs))
            return _FakeStateContext()

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(),
        raising=False,
    )
    original = AuthBrowser._inject_vault_cookies
    AuthBrowser._inject_vault_cookies = lambda self, **kw: injected.append(1)
    try:
        ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
        with caplog.at_level(_logging.WARNING):
            ab._open()
    finally:
        AuthBrowser._inject_vault_cookies = original
        from dpc_client_core.dpc_agent.tools.browser import _active_camoufox_browsers
        _active_camoufox_browsers.discard(ab)

    assert "storage_state" not in new_context_kwargs[0]
    assert injected == [1]
    assert any(
        "storage_state parse error" in rec.getMessage()
        for rec in caplog.records
    )


# ─────────────────────────────────────────────────────────────
# ADR-029 Task 004 follow-up — _auth_browse_html passes headed
# ─────────────────────────────────────────────────────────────


def test_auth_browse_html_defaults_to_headed_true(vault_home, monkeypatch):
    """auth path implies user-visible interaction (CAPTCHA, login follow-up)
    — _auth_browse_html defaults headed=True and forwards to AuthBrowser."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    captured: dict = {}

    class _StubAB:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def goto(self, url):
            pass

        def get_page_html(self):
            return "<html></html>"

    monkeypatch.setattr(mod, "AuthBrowser", _StubAB)
    mod._auth_browse_html("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/")
    assert captured.get("headed") is True


def test_auth_browse_html_respects_headed_false_override(vault_home, monkeypatch):
    """Caller can opt headless (e.g. CI / scripted scrape) by passing
    headed=False explicitly."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    captured: dict = {}

    class _StubAB:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def goto(self, url):
            pass

        def get_page_html(self):
            return ""

    monkeypatch.setattr(mod, "AuthBrowser", _StubAB)
    mod._auth_browse_html("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/", headed=False)
    assert captured.get("headed") is False


# ─────────────────────────────────────────────────────────────
# Cookie format conversion (snake_case → Playwright camelCase)
# ─────────────────────────────────────────────────────────────


def test_to_playwright_cookies_renames_keys():
    from dpc_client_core.dpc_agent.tools.browser import _to_playwright_cookies

    src = [
        {
            "name": "s", "value": "v",
            "domain": f".{TEST_DOMAIN}", "path": "/",
            "expires": 123, "secure": True,
            "httponly": True, "samesite": "Lax",
        }
    ]
    out = _to_playwright_cookies(src)
    assert len(out) == 1
    pc = out[0]
    assert pc["httpOnly"] is True
    assert pc["sameSite"] == "Lax"
    assert "httponly" not in pc
    assert "samesite" not in pc


def test_to_playwright_cookies_omits_session_expires():
    """Session cookies have expires=None — Playwright add_cookies rejects
    None for `expires`, so the key must be omitted entirely."""
    from dpc_client_core.dpc_agent.tools.browser import _to_playwright_cookies

    src = [{"name": "s", "value": "v", "domain": f".{TEST_DOMAIN}", "expires": None}]
    out = _to_playwright_cookies(src)
    assert "expires" not in out[0]


def test_to_playwright_cookies_omits_empty_samesite():
    from dpc_client_core.dpc_agent.tools.browser import _to_playwright_cookies

    src = [{"name": "s", "value": "v", "domain": f".{TEST_DOMAIN}", "samesite": None}]
    out = _to_playwright_cookies(src)
    assert "sameSite" not in out[0]


# ─────────────────────────────────────────────────────────────
# browse_page integration — use_auth path returns re-login prompt
# ─────────────────────────────────────────────────────────────


def _make_ctx(agent_root: Path):
    """Minimal ToolContext stub — only agent_root is read by browse_page
    when use_auth is set."""
    ns = types.SimpleNamespace()
    ns.agent_root = agent_root
    return ns


def test_browse_page_use_auth_returns_relogin_on_auth_required(vault_home):
    """When the auth path raises AuthRequiredError (cookies missing or
    rejected at a protected resource), browse_page must surface it as a
    ⚠️ re-login prompt — not a raw stack trace.

    Hermetic: _auth_browse_html is stubbed to raise, so no real Camoufox /
    network is touched. Note the empty vault alone no longer triggers this:
    AuthBrowser.open() is tolerant of missing cookies (skip_missing=True in
    browser.py — re-login surfaces only when a protected request is
    rejected), so this test drives the error path directly.
    """
    import dpc_client_core.dpc_agent.tools.browser as mod
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthRequiredError,
        browse_page,
    )

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)

    def _raise_auth_required(agent_id, domain, url, headed=True):
        raise AuthRequiredError(
            f"Auth required for {domain}: please re-login via the web-auth UI."
        )

    original = mod._auth_browse_html
    mod._auth_browse_html = _raise_auth_required
    try:
        out = asyncio.run(
            browse_page(
                ctx,
                url=f"https://{TEST_DOMAIN}/my/orders",
                use_auth=f"{TEST_DOMAIN}",
            )
        )
    finally:
        mod._auth_browse_html = original
    assert out.startswith("⚠️")
    assert f"{TEST_DOMAIN}" in out
    assert "re-login" in out.lower()


def test_browse_page_use_auth_rejects_off_domain_url(vault_home, fresh_cookies):
    """When use_auth is set and the URL is outside the auth domain,
    browse_page returns a warning rather than launching the browser."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import browse_page

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    ctx = _make_ctx(agent_root)
    # Patch _auth_browse_html to simulate AuthBrowser raising ValueError
    # from the domain check (we can't open Camoufox in the test runner).
    # T9 split moved the auth-path entry point from _auth_browse to
    # _auth_browse_html (raw HTML before trafilatura).
    import dpc_client_core.dpc_agent.tools.browser as mod

    def _raise_domain_mismatch(agent_id, domain, url, headed=True):
        raise ValueError(f"URL {url!r} is outside auth domain f'{TEST_DOMAIN}'")

    original = mod._auth_browse_html
    mod._auth_browse_html = _raise_domain_mismatch
    try:
        out = asyncio.run(
            browse_page(ctx, url="https://example.net/search", use_auth=f"{TEST_DOMAIN}")
        )
    finally:
        mod._auth_browse_html = original
    assert out.startswith("⚠️")
    assert "outside auth domain" in out


# ─────────────────────────────────────────────────────────────
# Shutdown fallback — force-kill orphaned Camoufox subprocess
# ─────────────────────────────────────────────────────────────


def test_capture_browser_pids_records_new_children(vault_home):
    """_capture_browser_pids diffs the process's children so the browser
    subprocess tree spawned during launch is tracked. Verified with a real
    short-lived child (no Camoufox needed)."""
    import subprocess
    import sys
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    before = ab._snapshot_child_pids()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ab._capture_browser_pids(before)
        assert proc.pid in ab._browser_pids
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_force_kill_process_terminates_captured_pids(vault_home):
    """_force_kill_process kills the tree recorded in _browser_pids — the
    shutdown fallback for a Camoufox close that hangs on a dead driver.
    Verified with a real child so no Camoufox is launched."""
    import subprocess
    import sys
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert proc.poll() is None  # child is alive
        ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
        ab._browser_pids = {proc.pid}
        ab._force_kill_process()
        # terminate + 2s wait + kill window; wait() raises if still alive
        proc.wait(timeout=5)
        assert proc.returncode is not None
        assert ab._browser_pids == set()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_force_kill_process_noop_when_no_pids(vault_home):
    """No captured pids (headless / never-launched) → no-op, no raise."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    assert ab._browser_pids == set()
    ab._force_kill_process()  # must not raise
    assert ab._browser_pids == set()



# ─────────────────────────────────────────────────────────────
# navigate() must surface the HTTP status of an error page
# ─────────────────────────────────────────────────────────────


class _FakeResponsePage:
    def __init__(self, status: int, url: str):
        self.url = url
        self._status = status

    def goto(self, url, **kwargs):
        self.url = url
        return types.SimpleNamespace(status=self._status)


def _browser_for_navigate(status: int, monkeypatch):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}")
    ab._page = _FakeResponsePage(status, TEST_DOMAIN_URL)
    ab.audit: list = []
    monkeypatch.setattr(ab, "_check_domain", lambda url: None)
    monkeypatch.setattr(ab, "_wait_for_content_stable", lambda: None)
    monkeypatch.setattr(ab, "a11y_snapshot", lambda: ("button 'Subscribe'", {"@e1": {}}))
    monkeypatch.setattr(ab, "_save_storage_state", lambda: None)
    monkeypatch.setattr(
        ab, "_audit_action",
        lambda action, url, result, **kw: ab.audit.append((action, result, kw)),
    )
    return ab


def test_navigate_flags_http_error_page(vault_home, monkeypatch):
    """A 404 renders as an ordinary page and used to be indistinguishable
    from a real one, so the agent waited out full click timeouts on a
    page that never existed."""
    from dpc_client_core.dpc_agent.tools.browser import HTTP_ERROR_PREFIX

    ab = _browser_for_navigate(404, monkeypatch)
    out = ab.navigate(f"{TEST_DOMAIN_URL}/@nosuchchannel")
    assert out.startswith(f"{HTTP_ERROR_PREFIX}404")
    assert "Subscribe" in out
    assert ab.audit[0][2]["status"] == 404


def test_navigate_leaves_ok_page_unprefixed(vault_home, monkeypatch):
    from dpc_client_core.dpc_agent.tools.browser import HTTP_ERROR_PREFIX

    ab = _browser_for_navigate(200, monkeypatch)
    out = ab.navigate(f"{TEST_DOMAIN_URL}/@real")
    assert not out.startswith(HTTP_ERROR_PREFIX)
    assert ab.audit[0][2]["status"] == 200


# ─────────────────────────────────────────────────────────────
# S17 browser-lifecycle fixes: a transient navigation failure must not
# cost the browser; browse_page must stop launching one per call; the
# auth path must honour the headless it asked approval for.
# ─────────────────────────────────────────────────────────────


class _FakeSession:
    """Stands in for AuthBrowser on the `_run_in_session` contract:
    a `_get_executor()` plus plain sync methods."""

    def __init__(self, fail_with=None, fail_times=0):
        self._last_activity = 0.0
        self._page = object()
        self._agent_id = "agent_a"
        self.calls: list = []
        self.closed = False
        self._fail_with = fail_with
        self._fail_times = fail_times

    def _get_executor(self):
        return None  # default loop executor is fine for a sync stub

    def navigate(self, url):
        self.calls.append(("navigate", url))
        if self._fail_times > 0:
            self._fail_times -= 1
            raise self._fail_with
        return "snapshot"

    def fetch_html(self, url):
        self.calls.append(("fetch_html", url))
        return "<html><body><p>pooled</p></body></html>"

    def close(self):
        self.closed = True
        self.calls.append(("close", None))


_TRANSIENT = Exception(
    "Page.goto: Navigation to https://a/x is interrupted by "
    "another navigation to https://a/y"
)
_DEAD = Exception("Target page, context or browser has been closed")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (_TRANSIENT, False),
        (Exception("Timeout 60000ms exceeded"), False),
        (Exception("net::ERR_ABORTED"), False),
        (_DEAD, True),
        (Exception("Browser closed"), True),
        (Exception("Connection closed while reading from the driver"), True),
    ],
)
def test_is_session_dead_classification(exc, expected):
    from dpc_client_core.dpc_agent.tools.browser import _is_session_dead

    assert _is_session_dead(exc) is expected


def test_navigate_retries_in_place_on_transient_failure():
    """A navigation that loses a race leaves a working browser. Recovery
    must retry on the same page, not tear the session down."""
    from dpc_client_core.dpc_agent.tools.browser import _navigate_with_recovery

    session = _FakeSession(fail_with=_TRANSIENT, fail_times=1)
    out = asyncio.run(
        _navigate_with_recovery(session, "agent_a", "https://a/x", [])
    )

    assert out is session, "same session must keep serving"
    assert session.closed is False, "a live browser must not be closed"
    assert [c[0] for c in session.calls] == ["navigate", "navigate"]


def test_navigate_recycles_when_session_is_dead(monkeypatch):
    """The one case that does justify a relaunch: the browser is gone."""
    import dpc_client_core.dpc_agent.tools.browser as mod

    dead = _FakeSession(fail_with=_DEAD, fail_times=1)
    fresh = _FakeSession()

    async def _fake_create(agent_id, domains, headed):
        return fresh

    monkeypatch.setattr(mod, "_get_or_create_session_async", _fake_create)
    out = asyncio.run(
        mod._navigate_with_recovery(dead, "agent_a", "https://a/x", [])
    )

    assert out is fresh
    assert dead.closed is True
    assert [c[0] for c in fresh.calls] == ["navigate"]


def test_fetch_session_is_invisible_to_interactive_tools(vault_home):
    """The pooled fetch browser must never be handed to browser_* tools:
    they resolve `_active_browser_sessions`, and a fetch navigating the
    page an agent holds refs into would silently invalidate them."""
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        _active_browser_sessions,
        _fetch_sessions,
        _get_session_or_error,
    )

    interactive = AuthBrowser(agent_id="agent_a", domains=[])
    interactive._page = object()
    fetch = AuthBrowser(agent_id="agent_a", domains=[])
    fetch._page = object()
    _active_browser_sessions["agent_a"] = interactive
    _fetch_sessions["agent_a"] = fetch
    try:
        assert _get_session_or_error("agent_a") is interactive
    finally:
        _active_browser_sessions.pop("agent_a", None)
        _fetch_sessions.pop("agent_a", None)


def test_close_deregisters_only_its_own_entry(vault_home):
    """Two browsers can share one agent_id. Closing one must not evict
    the other's registration while that browser is still running."""
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        _active_browser_sessions,
        _fetch_sessions,
    )

    interactive = AuthBrowser(agent_id="agent_a", domains=[])
    fetch = AuthBrowser(agent_id="agent_a", domains=[])
    _active_browser_sessions["agent_a"] = interactive
    _fetch_sessions["agent_a"] = fetch
    try:
        fetch.close()
        assert _active_browser_sessions.get("agent_a") is interactive
        assert "agent_a" not in _fetch_sessions
        interactive.close()
        assert "agent_a" not in _active_browser_sessions
    finally:
        _active_browser_sessions.pop("agent_a", None)
        _fetch_sessions.pop("agent_a", None)


def test_fetch_js_text_reuses_pooled_browser(monkeypatch):
    """The JS fallback goes through the pooled browser, not a launch."""
    import dpc_client_core.dpc_agent.tools.browser as mod

    session = _FakeSession()
    launched: list = []

    async def _fake_pool(agent_id):
        return session

    monkeypatch.setattr(mod, "_get_or_create_fetch_session", _fake_pool)
    monkeypatch.setattr(
        mod, "_browse_with_camoufox",
        lambda url, agent_id="<anonymous>": launched.append((url, agent_id)),
    )
    monkeypatch.setattr(mod, "_html_to_markdown", lambda html: "pooled")

    out = asyncio.run(mod._fetch_js_text("https://a/x", "agent_a"))

    assert out == "pooled"
    assert session.calls == [("fetch_html", "https://a/x")]
    assert launched == [], "no one-shot browser may be launched"


def test_fetch_js_text_falls_back_to_one_shot(monkeypatch):
    """If the pool fails the tool must not get worse than it was — and
    the one-shot must carry the agent id, so its page console lines are
    attributable instead of landing as <anonymous>."""
    import dpc_client_core.dpc_agent.tools.browser as mod

    launched: list = []

    async def _boom(agent_id):
        raise RuntimeError("Camoufox launch failed")

    def _oneshot(url, agent_id="<anonymous>"):
        launched.append((url, agent_id))
        return "oneshot"

    monkeypatch.setattr(mod, "_get_or_create_fetch_session", _boom)
    monkeypatch.setattr(mod, "_browse_with_camoufox", _oneshot)

    out = asyncio.run(mod._fetch_js_text("https://a/x", "agent_a"))

    assert out == "oneshot"
    assert launched == [("https://a/x", "agent_a")]


def test_fetch_js_text_without_agent_uses_one_shot(monkeypatch):
    import dpc_client_core.dpc_agent.tools.browser as mod

    launched: list = []

    def _oneshot(url, agent_id="<anonymous>"):
        launched.append((url, agent_id))
        return "oneshot"

    monkeypatch.setattr(mod, "_browse_with_camoufox", _oneshot)

    out = asyncio.run(mod._fetch_js_text("https://a/x", None))

    assert out == "oneshot"
    assert launched == [("https://a/x", "<anonymous>")]


def test_browse_page_auth_without_keep_open_is_headless(vault_home):
    """The gate above this call asks the user to approve *headless*
    access and audits it as such; the browse must not be headed."""
    import dpc_client_core.dpc_agent.tools.browser as mod
    from dpc_client_core.dpc_agent.tools.browser import browse_page

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)

    seen: dict = {}

    def _capture(agent_id, domain, url, headed=True):
        seen["headed"] = headed
        return "<html><body><p>ok</p></body></html>"

    original = mod._auth_browse_html
    mod._auth_browse_html = _capture
    try:
        asyncio.run(
            browse_page(
                ctx,
                url=f"https://{TEST_DOMAIN}/my/orders",
                use_auth=f"{TEST_DOMAIN}",
            )
        )
    finally:
        mod._auth_browse_html = original

    assert seen["headed"] is False


def test_idle_cleanup_sweeps_fetch_browsers():
    """Fetch browsers are reaped by the same idle sweep as sessions —
    otherwise a headless browser would outlive every agent round."""
    from dpc_client_core.dpc_agent.tools.browser import (
        _fetch_sessions,
        cleanup_idle_browser_sessions,
    )

    stale = _FakeSession()
    stale._last_activity = time.monotonic() - 10_000
    _fetch_sessions["agent_a"] = stale
    try:
        closed = asyncio.run(cleanup_idle_browser_sessions())
        assert closed >= 1
        assert stale.closed is True
        assert "agent_a" not in _fetch_sessions
    finally:
        _fetch_sessions.pop("agent_a", None)



# ─────────────────────────────────────────────────────────────
# S17: refs address the element the snapshot walked, and the
# summarizer may not eat the only things the agent can act on.
# ─────────────────────────────────────────────────────────────


class _RecordingPage:
    """Captures what locator/get_by_role the resolver reaches for."""

    def __init__(self, count=1):
        self.locators: list = []
        self.roles: list = []
        self._count = count

    def locator(self, selector):
        self.locators.append(selector)
        page = self

        class _Loc:
            def count(self_inner):
                return page._count

        return _Loc()

    def get_by_role(self, role, name=None):
        self.roles.append((role, name))
        raise AssertionError("refs must not go through get_by_role any more")


def _browser_with_refs(refs, count=1):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[])
    ab._page = _RecordingPage(count=count)
    ab._last_refs = refs
    return ab


def test_ref_resolves_to_the_marked_element(vault_home):
    """The failure this replaces: a nameless icon button became
    get_by_role("button") with no disambiguation, matched all 24 buttons
    on the page and died instantly on strict mode."""
    ab = _browser_with_refs({"@e7": {"role": "button", "name": "", "el": "3:41"}})
    ab._resolve_ref("@e7")
    assert ab._page.locators == ['[data-dpc-el="3:41"]']
    assert ab._page.roles == []


def test_named_ref_also_resolves_by_mark(vault_home):
    """Names are not unique either, and the ordinal that disambiguated them
    assumed the page had not re-rendered since the snapshot."""
    ab = _browser_with_refs(
        {"@e2": {"role": "link", "name": "Integrity and Authenticity", "el": "5:9"}}
    )
    ab._resolve_ref("@e2")
    assert ab._page.locators == ['[data-dpc-el="5:9"]']


def test_stale_ref_fails_immediately_with_a_useful_message(vault_home):
    """A vanished element used to cost the full 30 s timeout and then be
    reported as if it were merely slow."""
    ab = _browser_with_refs(
        {"@e2": {"role": "link", "name": "x", "el": "5:9"}}, count=0,
    )
    with pytest.raises(ValueError) as exc:
        ab._resolve_ref("@e2")
    assert "stale" in str(exc.value)
    assert "a11y_snapshot" in str(exc.value)


def test_unknown_ref_still_raises(vault_home):
    ab = _browser_with_refs({})
    with pytest.raises(ValueError) as exc:
        ab._resolve_ref("@e1")
    assert "unknown ref" in str(exc.value)


def test_css_selector_passes_through(vault_home):
    ab = _browser_with_refs({})
    ab._resolve_ref("button.primary")
    assert ab._page.locators == ["button.primary"]


def test_snapshot_marks_are_scoped_per_snapshot(vault_home):
    """Marks left on elements a later walk no longer reaches must not be
    able to answer a current ref."""
    from dpc_client_core.dpc_agent.tools.browser import _A11Y_DOM_SNAPSHOT_JS

    assert _A11Y_DOM_SNAPSHOT_JS.lstrip().startswith("(serial)")
    assert "data-dpc-el" in _A11Y_DOM_SNAPSHOT_JS
    assert "serial + ':' + nodeCount" in _A11Y_DOM_SNAPSHOT_JS


# ─────────────────────────────────────────────────────────────
# Summarization keeps every ref
# ─────────────────────────────────────────────────────────────


_CALENDAR_SNAPSHOT = "\n".join(
    ["- dialog \"Schedule\"", "  - text \"Select a date\""]
    + [f'  - button "{d}" [@e{d}]' for d in range(1, 32)]
    + ["  - text \"Time zone\""]
)


def test_ref_lines_are_split_out_of_the_prose():
    from dpc_client_core.dpc_agent.tools.browser import _split_actionable_lines

    actionable, prose = _split_actionable_lines(_CALENDAR_SNAPSHOT)
    assert len(actionable) == 31
    assert "@e" not in prose
    assert "Select a date" in prose


def test_summary_keeps_every_ref_verbatim():
    """The observed loss: 31 day cells came back as one line of prose and the
    agent had nothing to click."""
    from dpc_client_core.dpc_agent.tools.browser import (
        _rejoin_with_actionable,
        _split_actionable_lines,
    )

    actionable, _prose = _split_actionable_lines(_CALENDAR_SNAPSHOT)
    out = _rejoin_with_actionable("Calendar showing August 2026 (day grid 1-31)", actionable)
    for d in range(1, 32):
        assert f"[@e{d}]" in out


def test_summarizer_is_only_shown_the_prose(monkeypatch):
    """What the auxiliary model never sees, it cannot drop."""
    import dpc_client_core.dpc_agent.tools.browser as mod

    seen = {}

    class _LLM:
        async def query(self, prompt, provider_alias=None):
            seen["prompt"] = prompt
            return "short summary"

    out = asyncio.run(
        mod._llm_summarize_snapshot(_CALENDAR_SNAPSHOT, None, _LLM(), max_chars=10)
    )
    # The template names @e5 as an illustration, so assert on the snapshot's
    # own refs rather than on the substring.
    assert "[@e" not in seen["prompt"], "ref lines must not reach the summarizer"
    assert out.count("[@e") == 31


def test_duplicate_names_address_different_elements(vault_home):
    """Kept from the earlier ordinal scheme, which existed because a card
    exposes the same accessible name on its thumbnail and its title link.
    Distinct marks answer it directly instead of by counting."""
    ab = _browser_with_refs({
        "@e1": {"role": "link", "name": "Deceased Flesh", "el": "2:10"},
        "@e2": {"role": "link", "name": "Deceased Flesh", "el": "2:14"},
    })
    ab._resolve_ref("@e1")
    ab._resolve_ref("@e2")
    assert ab._page.locators == ['[data-dpc-el="2:10"]', '[data-dpc-el="2:14"]']


def test_refs_survive_a_summarizer_timeout():
    import dpc_client_core.dpc_agent.tools.browser as mod

    class _HangingLLM:
        async def query(self, prompt, provider_alias=None):
            await asyncio.sleep(mod.SNAPSHOT_SUMMARIZE_TIMEOUT_SEC + 5)

    out = asyncio.run(
        mod._llm_summarize_snapshot(_CALENDAR_SNAPSHOT, None, _HangingLLM(), max_chars=10)
    )
    assert out.count("[@e") == 31


def test_refs_survive_without_an_llm_at_all():
    import dpc_client_core.dpc_agent.tools.browser as mod

    out = asyncio.run(
        mod._llm_summarize_snapshot(_CALENDAR_SNAPSHOT, None, None, max_chars=10)
    )
    assert out.count("[@e") == 31


# ─────────────────────────────────────────────────────────────
# The audit says why, not just that
# ─────────────────────────────────────────────────────────────


def test_audit_error_records_the_message():
    """`Error` alone covered a strict-mode violation naming 24 buttons and
    unrelated refusals; the record kept neither message."""
    from dpc_client_core.dpc_agent.tools.browser import _audit_error

    exc = RuntimeError(
        "Locator.click: Error: strict mode violation: "
        'get_by_role("button") resolved to 24 elements:\n'
        "  1) <button ...>\n  2) <button ...>"
    )
    fields = _audit_error(exc)
    assert fields["error"] == "RuntimeError"
    assert "strict mode violation" in fields["error_message"]
    assert "\n" not in fields["error_message"], "call log must not be inlined"
    assert len(fields["error_message"]) <= 300


def test_headless_gate_fails_fast_without_a_ui(vault_home):
    """The gate broadcasts a request and waits 120s for an answer. When no UI
    client is connected the broadcast is dropped, so the wait could only end
    in a timeout — and the agent was told "not approved", as though a human
    had refused. 19 requests across three agents expired that way before the
    dialog existed."""
    import time
    import dpc_client_core.dpc_agent.tools.browser as mod
    from dpc_client_core.dpc_agent.tools.browser import browse_page

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)

    broadcasts = []

    class _NoUiApi:
        has_clients = False

        async def broadcast_event(self, name, payload):
            broadcasts.append(name)

    ctx.dpc_service = types.SimpleNamespace(local_api=_NoUiApi())

    started = time.monotonic()
    out = asyncio.run(
        browse_page(ctx, url=f"https://{TEST_DOMAIN}/x", use_auth=f"{TEST_DOMAIN}")
    )
    elapsed = time.monotonic() - started

    assert "no UI client is connected" in out
    assert elapsed < 5, "must not wait out the 120s approval window"
    assert broadcasts == [], "no point broadcasting to nobody"


def test_headless_gate_still_waits_when_a_ui_is_connected(vault_home):
    """With a UI attached the request is real: broadcast, then wait for the
    answer the dialog sends back."""
    import dpc_client_core.dpc_agent.tools.browser as mod
    from dpc_client_core.dpc_agent.tools.browser import browse_page

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)

    broadcasts = []

    class _LiveApi:
        has_clients = True

        async def broadcast_event(self, name, payload):
            broadcasts.append(name)
            # Answer immediately, the way the dialog does.
            entry = mod.get_pending_auth_approvals()[payload["request_id"]]
            entry["approved"] = True
            entry["event"].set()

    ctx.dpc_service = types.SimpleNamespace(local_api=_LiveApi())

    def _html(agent_id, domain, url, headed=True):
        return "<html><body><p>ok</p></body></html>"

    original = mod._auth_browse_html
    mod._auth_browse_html = _html
    try:
        out = asyncio.run(
            browse_page(ctx, url=f"https://{TEST_DOMAIN}/x", use_auth=f"{TEST_DOMAIN}")
        )
    finally:
        mod._auth_browse_html = original

    assert broadcasts == ["web_auth_headless_approval_request"]
    assert "not approved" not in out


def test_fetch_browser_carries_no_login(vault_home):
    """The regression this closes: the browse_page JS fallback opened with
    the agent's whole saved login and ran as a second, concurrent browser
    against the same account — two sessions, two fingerprints, one set of
    Google/TikTok cookies. browse_page without use_auth is the
    unauthenticated path; it must carry no identity at all."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    fetch = AuthBrowser(agent_id="agent_a", domains=[], anonymous=True)
    assert fetch._anonymous is True

    interactive = AuthBrowser(agent_id="agent_a", domains=[])
    assert interactive._anonymous is False


def test_anonymous_browser_never_writes_the_shared_state(vault_home):
    """Second half of the same defect: both browsers resolve the same
    ~/.dpc/agents/{id}/browser_state.json, so an anonymous one closing last
    would overwrite the interactive session's login with a blank one."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    state = vault_home / "agents" / "agent_a" / "browser_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"cookies": [{"name": "SID"}], "origins": []}', encoding="utf-8")

    fetch = AuthBrowser(agent_id="agent_a", domains=[], anonymous=True)

    class _Ctx:
        def storage_state(self, **_kw):
            raise AssertionError("anonymous browser must not read state out")

    fetch._context = _Ctx()
    fetch._save_storage_state()  # must be a no-op, not an exception

    assert json.loads(state.read_text(encoding="utf-8"))["cookies"] == [{"name": "SID"}]


def test_save_does_not_collect_origins(vault_home):
    """Measured cause of the windows that opened and vanished: saving via
    storage_state() collects localStorage, and Firefox reads it by opening a
    window on each origin — a save with two origins peaked at two extra
    visible windows. This runs after every navigate and at close.

    Nothing is lost by skipping it: the load path strips origins before
    handing state to new_context, for the same reason in reverse."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    cookies = [{
        "name": "SID", "value": "v", "domain": f".{TEST_DOMAIN}",
        "path": "/", "secure": True, "httpOnly": True,
        "sameSite": "Lax", "expires": 1735689600,
    }]
    ctx = _FakeStateContext(cookies_payload=cookies)
    ab._context = ctx
    ab._save_storage_state()

    assert ctx.storage_state_calls == [], (
        "storage_state() opens a window per origin — the save must not call it"
    )
    assert ctx.cookies_calls == 1

    saved = json.loads(ab._state_path().read_text(encoding="utf-8"))
    assert saved["origins"] == []
    assert [c["name"] for c in saved["cookies"]] == ["SID"], "the login must survive"
