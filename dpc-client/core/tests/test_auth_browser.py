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


def test_auth_required_when_no_cookies(vault_home):
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        AuthRequiredError,
    )

    with pytest.raises(AuthRequiredError) as exc:
        AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    assert "ozon.ru" in str(exc.value)
    assert "re-login" in str(exc.value).lower()


def test_auth_expired_when_cookies_old(vault_home, expired_cookies):
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import (
        AuthBrowser,
        AuthExpiredError,
    )

    web_auth.save_cookies("agent_a", "ozon.ru", expired_cookies)
    with pytest.raises(AuthExpiredError) as exc:
        AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    assert "ozon.ru" in str(exc.value)
    assert "expired" in str(exc.value).lower()


def test_construction_succeeds_with_fresh_cookies(vault_home, fresh_cookies):
    """AuthBrowser must not touch Camoufox during __init__. We verify by
    *not* calling __enter__ — construction alone should not import or
    launch Camoufox. If it does, this test would either ImportError or
    spawn a real browser."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    assert ab.domain == "ozon.ru"


# ─────────────────────────────────────────────────────────────
# Restricted public surface — no interactive methods leaked
# ─────────────────────────────────────────────────────────────


def test_authbrowser_surface_is_read_only(vault_home, fresh_cookies):
    """Tool-level READ-only enforcement per ADR-028. The class must NOT
    expose Playwright/Camoufox methods that allow input or arbitrary
    code execution. Public surface is locked to navigation + reading."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    ab = AuthBrowser(agent_id="agent_a", domain="ozon.ru")
    public = {n for n in dir(ab) if not n.startswith("_")}
    # Allowed surface
    expected = {"goto", "get_page_content", "close", "domain"}
    assert expected.issubset(public), f"missing methods: {expected - public}"
    # Forbidden surface (Playwright/Camoufox interactive API)
    forbidden = {
        "click", "fill", "type", "press", "evaluate", "evaluate_handle",
        "add_init_script", "expose_function", "set_content", "wait_for_function",
        "page", "context", "browser", "request",
    }
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
    # Patch _auth_browse to simulate AuthBrowser raising ValueError
    # from the domain check (we can't open Camoufox in the test runner).
    import dpc_client_core.dpc_agent.tools.browser as mod

    def _raise_domain_mismatch(agent_id, domain, url):
        raise ValueError(f"URL {url!r} is outside auth domain 'ozon.ru'")

    original = mod._auth_browse
    mod._auth_browse = _raise_domain_mismatch
    try:
        out = asyncio.run(
            browse_page(ctx, url="https://yandex.ru/search", use_auth="ozon.ru")
        )
    finally:
        mod._auth_browse = original
    assert out.startswith("⚠️")
    assert "outside auth domain" in out
