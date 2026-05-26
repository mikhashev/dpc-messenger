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
    ("https://ozon.com/path", f"{TEST_DOMAIN}", False),
    # Different domain
    ("https://yandex.ru/", f"{TEST_DOMAIN}", False),
    # Adversarial — auth domain in path/query, not host
    (f"https://attacker.com/?ref={TEST_DOMAIN}", f"{TEST_DOMAIN}", False),
    (f"https://attacker.com/{TEST_DOMAIN}/page", f"{TEST_DOMAIN}", False),
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
        ab.goto("https://yandex.ru/search")
    assert "outside auth domain" in str(exc.value)


# ─────────────────────────────────────────────────────────────
# ADR-029 Task 002 — multi-domain init + session registry + headed
# ─────────────────────────────────────────────────────────────


def test_multi_domain_constructor(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[f"{TEST_DOMAIN}", "yandex.ru"], headed=True
    )
    assert set(ab.domains) == {f"{TEST_DOMAIN}", "yandex.ru"}
    assert ab.headed is True
    assert ab.domain == f"{TEST_DOMAIN}"  # back-compat scalar = first


def test_constructor_rejects_both_domain_and_domains(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    with pytest.raises(ValueError, match="domains.*or.*domain"):
        AuthBrowser(agent_id="agent_a", domain=f"{TEST_DOMAIN}", domains=["yandex.ru"])


def test_multi_domain_check_allows_any_etld1(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}", "yandex.ru"])
    ab._page = object()
    # Both allowed, no exception
    ab._check_domain(f"https://{TEST_DOMAIN}/orders")
    ab._check_domain("https://www.yandex.ru/search")
    # Off-domain still rejected
    with pytest.raises(ValueError, match="outside auth domains"):
        ab._check_domain("https://attacker.com/")


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
    substring (notozon.ru, fake-ozon.ru) must be aborted — eTLD+1
    resolution, not substring match."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}"])
    for hostile in (
        f"https://not{TEST_DOMAIN}/payload",
        f"https://fake-{TEST_DOMAIN}/payload",
        f"https://attacker.com/?ref={TEST_DOMAIN}",
    ):
        route = _FakeRoute(hostile)
        ab._domain_route_gate(route)
        assert route.aborted is True, hostile
        assert route.continued is False, hostile
    assert ab._domain_blocks == 3


def test_route_gate_multi_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[f"{TEST_DOMAIN}", "yandex.ru"])
    for ok in (
        f"https://{TEST_DOMAIN}/orders",
        "https://www.yandex.ru/search",
        f"https://api.{TEST_DOMAIN}/v2/x",
    ):
        route = _FakeRoute(ok)
        ab._domain_route_gate(route)
        assert route.continued is True, ok
    route = _FakeRoute("https://google.com/search")
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

    def __init__(self, on_storage_state=None):
        self.added: list[list[dict]] = []
        self.routes: list[tuple[str, object]] = []
        self.pages: list[object] = []
        self._on_storage_state = on_storage_state
        self.closed = False
        self.storage_state_calls: list[str] = []

    def add_cookies(self, cookies: list[dict]) -> None:
        self.added.append(list(cookies))

    def route(self, pattern: str, handler) -> None:
        self.routes.append((pattern, handler))

    def new_page(self):
        page = object()
        self.pages.append(page)
        return page

    def storage_state(self, path: str | None = None) -> dict | None:
        self.storage_state_calls.append(path or "<no-path>")
        if self._on_storage_state is not None:
            # Real Playwright returns the state dict; allow the test
            # callback to return one too so we can exercise the
            # return-value path that skips the read-back.
            return self._on_storage_state(path)
        return None

    def close(self) -> None:
        self.closed = True


def test_from_playwright_cookies_drops_session_marker(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import _from_playwright_cookies

    src = [
        {"name": "lang", "value": "en", "domain": ".x.com", "expires": -1},
        {"name": "session", "value": "x", "domain": ".x.com"},
        {"name": "valid", "value": "v", "domain": ".x.com", "expires": 1735689600},
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
            "name": "leak", "value": "3", "domain": ".google.com", "path": "/",
            "secure": True, "httpOnly": True,
        },
    ]
    ab._sync_cookies_to_vault(state_cookies)
    saved = web_auth.load_cookies("agent_a", f"{TEST_DOMAIN}")
    assert saved is not None
    names = sorted(c["name"] for c in saved)
    assert names == ["a", "b"]
    # google.com cookie not saved anywhere
    assert web_auth.load_cookies("agent_a", "google.com") is None


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

    ab._context = _FakeStateContext(on_storage_state=_write_state)
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
        ab._context = _FakeStateContext(on_storage_state=_write_state)
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

    ab._context = _FakeStateContext(on_storage_state=_write_state)
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

    ab._context = _FakeStateContext(on_storage_state=_write_state)
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

    ab._context = _FakeStateContext(on_storage_state=_write_state)
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
    AuthBrowser._inject_vault_cookies = lambda self: injected.append(1)
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
    assert injected == []  # vault path NOT taken when state file used


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
    AuthBrowser._inject_vault_cookies = lambda self: injected.append(1)
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
    AuthBrowser._inject_vault_cookies = lambda self: injected.append(1)
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


def test_browse_page_use_auth_returns_relogin_when_vault_empty(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import browse_page

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)
    out = asyncio.run(
        browse_page(ctx, url=f"https://{TEST_DOMAIN}/my/orders", use_auth=f"{TEST_DOMAIN}")
    )
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
            browse_page(ctx, url="https://yandex.ru/search", use_auth=f"{TEST_DOMAIN}")
        )
    finally:
        mod._auth_browse_html = original
    assert out.startswith("⚠️")
    assert "outside auth domain" in out


# ─────────────────────────────────────────────────────────────
# ADR-028 T9 — Popup fallback (caller side)
# ─────────────────────────────────────────────────────────────


class _FakeLocalApi:
    """Captures broadcast_event payloads for assertions."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def broadcast_event(self, name: str, payload: dict):
        self.events.append((name, payload))


def _make_ctx_with_local_api(agent_root: Path, local_api):
    """ToolContext with dpc_service.local_api wired (no firewall)."""
    ns = types.SimpleNamespace()
    ns.agent_root = agent_root
    ns.dpc_service = types.SimpleNamespace()
    ns.dpc_service.firewall = None
    ns.dpc_service.local_api = local_api
    return ns


def test_request_popup_fallback_without_dpc_service_raises():
    """No dpc_service in ctx → AuthRequiredError, no broadcast attempt."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    ctx = types.SimpleNamespace()
    ctx.agent_root = Path("/tmp/agent_a")
    # No dpc_service attribute at all.

    async def _run():
        with pytest.raises(mod.AuthRequiredError, match="DPC service"):
            await mod._request_popup_fallback(
                ctx, "agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/my"
            )

    asyncio.run(_run())


def test_request_popup_fallback_happy_path_resolves_with_html(tmp_path):
    """broadcast_event fires with right payload; future resolution from
    Step-3 handler returns the popup-extracted HTML to the caller."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    api = _FakeLocalApi()
    ctx = _make_ctx_with_local_api(tmp_path / "agent_a", api)

    async def _run():
        # Simulate Step-3 handler: wait one tick, then resolve the
        # pending future by id with the popup HTML.
        async def _resolve_later():
            await asyncio.sleep(0)
            pending = mod.get_pending_popup_requests()
            assert len(pending) == 1, "exactly one request registered"
            (request_id, entry), = pending.items()
            # Sanity-check the PendingPopupRequest the caller registered.
            assert entry.expected_url == f"https://{TEST_DOMAIN}/my"
            assert entry.expected_etld1 == f"{TEST_DOMAIN}"
            entry.future.set_result("<html><body>popup html</body></html>")

        resolver = asyncio.create_task(_resolve_later())
        out = await mod._request_popup_fallback(
            ctx, "agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/my"
        )
        await resolver
        return out

    out = asyncio.run(_run())
    assert out == "<html><body>popup html</body></html>"

    # Broadcast inspection.
    assert len(api.events) == 1
    name, payload = api.events[0]
    assert name == "web_auth_popup_request"
    assert payload["agent_id"] == "agent_a"
    assert payload["domain"] == f"{TEST_DOMAIN}"
    assert payload["url"] == f"https://{TEST_DOMAIN}/my"
    assert payload["reason"] == "anti_bot_challenge"
    assert "request_id" in payload and payload["request_id"]

    # Pending dict cleared after success.
    assert mod.get_pending_popup_requests() == {}


def test_request_popup_fallback_timeout_raises_auth_required(tmp_path, monkeypatch):
    """No frontend response within timeout → AuthRequiredError; pending
    dict cleared so subsequent retries don't leak."""
    from dpc_client_core.dpc_agent.tools import browser as mod

    monkeypatch.setattr(mod, "_POPUP_TIMEOUT_S", 0.05)  # 50ms for the test
    api = _FakeLocalApi()
    ctx = _make_ctx_with_local_api(tmp_path / "agent_a", api)

    async def _run():
        with pytest.raises(mod.AuthRequiredError, match="timeout"):
            await mod._request_popup_fallback(
                ctx, "agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/my"
            )

    asyncio.run(_run())
    assert mod.get_pending_popup_requests() == {}


def test_browse_page_challenge_triggers_popup_fallback(vault_home, fresh_cookies):
    """Camoufox returns anti-bot stub → looks_like_challenge True →
    _request_popup_fallback called → returns popup HTML → markdown."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as mod

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    api = _FakeLocalApi()
    ctx = _make_ctx_with_local_api(agent_root, api)

    challenge_html = (
        "<html><body><script>window.__cf_chl_opt={}</script></body></html>"
    )
    popup_html = "<html><body><h1>After challenge</h1><p>Real content.</p></body></html>"

    original_html = mod._auth_browse_html
    mod._auth_browse_html = lambda *a, **kw: challenge_html

    original_popup = mod._request_popup_fallback

    async def _fake_popup(ctx, agent_id, domain, url):
        # Verify the caller passes through the right identifiers.
        assert agent_id == "agent_a"
        assert domain == f"{TEST_DOMAIN}"
        assert url == f"https://{TEST_DOMAIN}/my/orders"
        return popup_html

    mod._request_popup_fallback = _fake_popup
    try:
        out = asyncio.run(
            mod.browse_page(
                ctx, url=f"https://{TEST_DOMAIN}/my/orders", use_auth=f"{TEST_DOMAIN}"
            )
        )
    finally:
        mod._auth_browse_html = original_html
        mod._request_popup_fallback = original_popup

    # Output is markdown derived from popup_html, NOT challenge_html.
    assert "After challenge" in out
    assert "Real content" in out
    assert "__cf_chl_opt" not in out


def test_browse_page_challenge_popup_timeout_returns_warning(vault_home, fresh_cookies):
    """Popup fallback timeout → ⚠️ message bubbled to agent."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as mod

    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    api = _FakeLocalApi()
    ctx = _make_ctx_with_local_api(agent_root, api)

    challenge_html = "<html><body><div class=\"g-recaptcha\"></div></body></html>"

    original_html = mod._auth_browse_html
    mod._auth_browse_html = lambda *a, **kw: challenge_html

    original_popup = mod._request_popup_fallback

    async def _timeout_popup(ctx, agent_id, domain, url):
        raise mod.AuthRequiredError(
            f"Popup fallback timeout (300s) for https://{TEST_DOMAIN}/my — "
            "user did not complete"
        )

    mod._request_popup_fallback = _timeout_popup
    try:
        out = asyncio.run(
            mod.browse_page(ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=f"{TEST_DOMAIN}")
        )
    finally:
        mod._auth_browse_html = original_html
        mod._request_popup_fallback = original_popup

    assert out.startswith("⚠️")
    assert "timeout" in out.lower()
