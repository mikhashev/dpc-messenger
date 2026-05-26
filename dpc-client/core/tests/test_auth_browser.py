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

import asyncio
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
            "domain": ".ozon.ru",
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
            "domain": ".ozon.ru",
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

    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    with pytest.raises(AuthRequiredError) as exc:
        ab._load_all_cookies()
    assert "ozon.ru" in str(exc.value)
    assert "re-login" in str(exc.value).lower()


def test_auth_expired_raised_on_load(vault_home, expired_cookies):
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        AuthExpiredError,
    )

    web_auth.save_cookies("agent_a", "ozon.ru", expired_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    with pytest.raises(AuthExpiredError) as exc:
        ab._load_all_cookies()
    assert "ozon.ru" in str(exc.value)
    assert "expired" in str(exc.value).lower()


def test_construction_is_lazy(vault_home):
    """ADR-029: AuthBrowser construction does no I/O — no keyring read,
    no Camoufox import, no browser launch. Verifies the lazy contract
    that makes per-agent session registry safe to instantiate."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    assert ab.domain == "ozon.ru"
    assert ab.domains == ["ozon.ru"]
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

    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
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
    ("https://ozon.ru/path", "ozon.ru", True),
    # Subdomain
    ("https://www.ozon.ru/path", "ozon.ru", True),
    ("https://login.ozon.ru/oauth", "ozon.ru", True),
    ("https://api.ozon.ru/v2/orders", "ozon.ru", True),
    # Different TLD
    ("https://ozon.com/path", "ozon.ru", False),
    # Different domain
    ("https://yandex.ru/", "ozon.ru", False),
    # Adversarial — auth domain in path/query, not host
    ("https://attacker.com/?ref=ozon.ru", "ozon.ru", False),
    ("https://attacker.com/ozon.ru/page", "ozon.ru", False),
    # Adversarial — auth domain as suffix-LIKE but not subdomain
    ("https://notozon.ru/", "ozon.ru", False),
    ("https://fake-ozon.ru/", "ozon.ru", False),
    # Malformed
    ("not-a-url", "ozon.ru", False),
    ("", "ozon.ru", False),
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

    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
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
        agent_id="agent_a", domains=["ozon.ru", "yandex.ru"], headed=True
    )
    assert set(ab.domains) == {"ozon.ru", "yandex.ru"}
    assert ab.headed is True
    assert ab.domain == "ozon.ru"  # back-compat scalar = first


def test_constructor_rejects_both_domain_and_domains(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    with pytest.raises(ValueError, match="domains.*or.*domain"):
        AuthBrowser(agent_id="agent_a", domain="ozon.ru", domains=["yandex.ru"])


def test_multi_domain_check_allows_any_etld1(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru", "yandex.ru"])
    ab._page = object()
    # Both allowed, no exception
    ab._check_domain("https://ozon.ru/orders")
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

    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    # Stub a live session so the guard sees `_page is not None`
    stub = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
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

    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
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

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
    ab._context = _FakeContext()
    ab._install_domain_route_handler()
    assert len(ab._context.registered) == 1
    pattern, _handler = ab._context.registered[0]
    assert pattern == "**/*"


def test_route_gate_allows_in_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
    route = _FakeRoute("https://ozon.ru/my/orders")
    ab._domain_route_gate(route)
    assert route.continued is True
    assert route.aborted is False
    assert ab._domain_blocks == 0


def test_route_gate_allows_subdomain(vault_home):
    """Subdomain CDN of an allowed eTLD+1 must pass — page-asset case
    from spec (cdn.shop.example.com when shop.example.com whitelisted)."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
    route = _FakeRoute("https://cdn.ozon.ru/static/lib.js")
    ab._domain_route_gate(route)
    assert route.continued is True
    assert route.aborted is False


def test_route_gate_blocks_off_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
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

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
    for hostile in (
        "https://notozon.ru/payload",
        "https://fake-ozon.ru/payload",
        "https://attacker.com/?ref=ozon.ru",
    ):
        route = _FakeRoute(hostile)
        ab._domain_route_gate(route)
        assert route.aborted is True, hostile
        assert route.continued is False, hostile
    assert ab._domain_blocks == 3


def test_route_gate_multi_domain(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru", "yandex.ru"])
    for ok in (
        "https://ozon.ru/orders",
        "https://www.yandex.ru/search",
        "https://api.ozon.ru/v2/x",
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
    route = _FakeRoute("https://ozon.ru/my")
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False
    assert ab._domain_blocks == 1


def test_route_gate_allows_non_http_schemes(vault_home):
    """data:, about:, blob: URIs do not trigger external network
    requests — short-circuit ALLOW per spec."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
    for uri in (
        "data:text/html,<html><body>x</body></html>",
        "about:blank",
        "blob:https://ozon.ru/abc-123",
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

    ab = AuthBrowser(agent_id="agent_a", domains=["ozon.ru"])
    route = _BrokenRoute()
    ab._domain_route_gate(route)
    assert route.aborted is True
    assert route.continued is False


# ─────────────────────────────────────────────────────────────
# Cookie format conversion (snake_case → Playwright camelCase)
# ─────────────────────────────────────────────────────────────


def test_to_playwright_cookies_renames_keys():
    from dpc_client_core.dpc_agent.tools.browser import _to_playwright_cookies

    src = [
        {
            "name": "s", "value": "v",
            "domain": ".ozon.ru", "path": "/",
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

    src = [{"name": "s", "value": "v", "domain": ".ozon.ru", "expires": None}]
    out = _to_playwright_cookies(src)
    assert "expires" not in out[0]


def test_to_playwright_cookies_omits_empty_samesite():
    from dpc_client_core.dpc_agent.tools.browser import _to_playwright_cookies

    src = [{"name": "s", "value": "v", "domain": ".ozon.ru", "samesite": None}]
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
        browse_page(ctx, url="https://ozon.ru/my/orders", use_auth="ozon.ru")
    )
    assert out.startswith("⚠️")
    assert "ozon.ru" in out
    assert "re-login" in out.lower()


def test_browse_page_use_auth_rejects_off_domain_url(vault_home, fresh_cookies):
    """When use_auth is set and the URL is outside the auth domain,
    browse_page returns a warning rather than launching the browser."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import browse_page

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    ctx = _make_ctx(agent_root)
    # Patch _auth_browse_html to simulate AuthBrowser raising ValueError
    # from the domain check (we can't open Camoufox in the test runner).
    # T9 split moved the auth-path entry point from _auth_browse to
    # _auth_browse_html (raw HTML before trafilatura).
    import dpc_client_core.dpc_agent.tools.browser as mod

    def _raise_domain_mismatch(agent_id, domain, url):
        raise ValueError(f"URL {url!r} is outside auth domain 'ozon.ru'")

    original = mod._auth_browse_html
    mod._auth_browse_html = _raise_domain_mismatch
    try:
        out = asyncio.run(
            browse_page(ctx, url="https://yandex.ru/search", use_auth="ozon.ru")
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
                ctx, "agent_a", "ozon.ru", "https://ozon.ru/my"
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
            assert entry.expected_url == "https://ozon.ru/my"
            assert entry.expected_etld1 == "ozon.ru"
            entry.future.set_result("<html><body>popup html</body></html>")

        resolver = asyncio.create_task(_resolve_later())
        out = await mod._request_popup_fallback(
            ctx, "agent_a", "ozon.ru", "https://ozon.ru/my"
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
    assert payload["domain"] == "ozon.ru"
    assert payload["url"] == "https://ozon.ru/my"
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
                ctx, "agent_a", "ozon.ru", "https://ozon.ru/my"
            )

    asyncio.run(_run())
    assert mod.get_pending_popup_requests() == {}


def test_browse_page_challenge_triggers_popup_fallback(vault_home, fresh_cookies):
    """Camoufox returns anti-bot stub → looks_like_challenge True →
    _request_popup_fallback called → returns popup HTML → markdown."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as mod

    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
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
        assert domain == "ozon.ru"
        assert url == "https://ozon.ru/my/orders"
        return popup_html

    mod._request_popup_fallback = _fake_popup
    try:
        out = asyncio.run(
            mod.browse_page(
                ctx, url="https://ozon.ru/my/orders", use_auth="ozon.ru"
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

    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
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
            "Popup fallback timeout (300s) for https://ozon.ru/my — "
            "user did not complete"
        )

    mod._request_popup_fallback = _timeout_popup
    try:
        out = asyncio.run(
            mod.browse_page(ctx, url="https://ozon.ru/my", use_auth="ozon.ru")
        )
    finally:
        mod._auth_browse_html = original_html
        mod._request_popup_fallback = original_popup

    assert out.startswith("⚠️")
    assert "timeout" in out.lower()
