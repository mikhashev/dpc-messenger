"""Tests for ADR-028 T5: firewall.is_auth_domain_allowed (per-agent + per-domain gate).

Covers the firewall layer that gates `browse_page(use_auth=...)`:

  - Empty / missing whitelist → denied
  - Domain not in whitelist → denied
  - Whitelist OK but vault empty → denied
  - Whitelist OK + cookies present + not expired → allowed
  - Whitelist OK + cookies present + expired → denied
  - Per-agent isolation (agent_a allowed, agent_b denied)
  - eTLD+1 normalization (whitelist 'ozon.ru' admits 'www.ozon.ru')
  - browse_page integration: firewall denial returns ⚠️ before browser launch
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path

import pytest


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    """Isolated DPC_HOME + in-memory keyring (same shape as test_web_auth)."""
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
        {"name": "session_id", "value": "abc", "domain": ".ozon.ru",
         "path": "/", "expires": future, "secure": True,
         "httponly": True, "samesite": "Lax"},
    ]


@pytest.fixture
def expired_cookies():
    past = int(time.time()) - 3600
    return [
        {"name": "session_id", "value": "abc", "domain": ".ozon.ru",
         "path": "/", "expires": past, "secure": True,
         "httponly": True, "samesite": "Lax"},
    ]


def _make_firewall(tmp_path: Path, rules: dict):
    """Build a ContextFirewall pointed at a temp rules file."""
    from dpc_client_core.firewall import ContextFirewall

    rules_file = tmp_path / "privacy_rules.json"
    rules_file.write_text(json.dumps(rules), encoding="utf-8")
    return ContextFirewall(rules_file)


# ─────────────────────────────────────────────────────────────
# Whitelist gate (no cookies considered yet)
# ─────────────────────────────────────────────────────────────


def test_no_agent_profile_denies(vault_home):
    fw = _make_firewall(vault_home, {"agent_profiles": {}})
    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is False


def test_empty_whitelist_denies(vault_home):
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}}
    fw = _make_firewall(vault_home, rules)
    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is False


def test_domain_not_in_whitelist_denies(vault_home, fresh_cookies):
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "yandex.ru", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", "yandex.ru") is False


# ─────────────────────────────────────────────────────────────
# Cookie presence + expiry
# ─────────────────────────────────────────────────────────────


def test_whitelisted_but_no_cookies_denies(vault_home):
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is False


def test_whitelisted_and_fresh_cookies_allows(vault_home, fresh_cookies):
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is True


def test_whitelisted_but_expired_cookies_denies(vault_home, expired_cookies):
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", expired_cookies)
    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is False


# ─────────────────────────────────────────────────────────────
# Per-agent isolation (Mike S140 [#54] requirement)
# ─────────────────────────────────────────────────────────────


def test_per_agent_isolation(vault_home, fresh_cookies):
    """agent_a is authorized for ozon.ru, agent_b is not. Both have
    cookies in their vaults — only agent_a passes the firewall."""
    from dpc_client_core import web_auth

    rules = {
        "agent_profiles": {
            "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}},
            "agent_b": {"web_auth": {"allowed_domains": []}},
        }
    }
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    web_auth.save_cookies("agent_b", "ozon.ru", fresh_cookies)

    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is True
    assert fw.is_auth_domain_allowed("agent_b", "ozon.ru") is False


# ─────────────────────────────────────────────────────────────
# eTLD+1 normalization (subdomain accepted via whitelist root)
# ─────────────────────────────────────────────────────────────


def test_whitelist_case_insensitive(vault_home, fresh_cookies):
    """Hand-edited privacy_rules.json may have mixed-case entries like
    'Ozon.RU'. The whitelist comparison must be case-insensitive — the
    requested domain is already lowercased by resolve_etld1, the
    whitelist is now also normalized before the `in` check."""
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["Ozon.RU"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", "ozon.ru") is True
    assert fw.is_auth_domain_allowed("agent_a", "www.ozon.ru") is True


def test_subdomain_admitted_via_etld1(vault_home, fresh_cookies):
    """Whitelist contains the eTLD+1 'ozon.ru'. Requesting 'www.ozon.ru'
    or 'login.ozon.ru' should resolve to the same eTLD+1 and pass."""
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    # Cookies saved under the root domain (T3 already resolves eTLD+1)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)

    assert fw.is_auth_domain_allowed("agent_a", "www.ozon.ru") is True
    assert fw.is_auth_domain_allowed("agent_a", "login.ozon.ru") is True


# ─────────────────────────────────────────────────────────────
# Browse_page integration: firewall denial returns warning
# ─────────────────────────────────────────────────────────────


def _make_ctx_with_firewall(agent_root: Path, firewall):
    """Stub ToolContext with dpc_service.firewall wired."""
    ctx = types.SimpleNamespace()
    ctx.agent_root = agent_root
    ctx.dpc_service = types.SimpleNamespace()
    ctx.dpc_service.firewall = firewall
    return ctx


def test_browse_page_firewall_denial_returns_warning(vault_home, fresh_cookies):
    """browse_page(use_auth=...) with firewall denial returns ⚠️ message
    and does NOT call the authenticated browse path (no Camoufox launch)."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx_with_firewall(agent_root, fw)

    # Sentinel: if _auth_browse_html runs the firewall failed to block.
    called = {"value": False}
    original = browser_mod._auth_browse_html

    def _sentinel(*args, **kwargs):
        called["value"] = True
        return "<html><body>should not see this</body></html>"

    browser_mod._auth_browse_html = _sentinel
    try:
        out = asyncio.run(
            browser_mod.browse_page(
                ctx, url="https://ozon.ru/my/orders", use_auth="ozon.ru"
            )
        )
    finally:
        browser_mod._auth_browse_html = original

    assert called["value"] is False, "firewall failed to block _auth_browse_html"
    assert out.startswith("⚠️")
    assert "agent_a" in out
    assert "allowed_domains" in out


def test_browse_page_firewall_pass_proceeds_to_auth_browse(vault_home, fresh_cookies):
    """browse_page(use_auth=...) with firewall passing reaches the
    authenticated browse path (_auth_browse_html after T9 split)."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx_with_firewall(agent_root, fw)

    captured = {"called": False}
    original = browser_mod._auth_browse_html

    def _sentinel(agent_id, domain, url):
        captured["called"] = True
        captured["agent_id"] = agent_id
        captured["domain"] = domain
        return "<html><body>stub-page-content</body></html>"

    browser_mod._auth_browse_html = _sentinel
    try:
        out = asyncio.run(
            browser_mod.browse_page(
                ctx, url="https://ozon.ru/my/orders", use_auth="ozon.ru"
            )
        )
    finally:
        browser_mod._auth_browse_html = original

    assert captured["called"] is True
    assert captured["agent_id"] == "agent_a"
    assert captured["domain"] == "ozon.ru"
    assert "stub-page-content" in out


# ─────────────────────────────────────────────────────────────
# T9 always_popup (YarchePlus variant C)
# ─────────────────────────────────────────────────────────────


def test_get_agent_always_popup_domains_empty_default(vault_home):
    """Missing always_popup field → empty list (the headless-fetch path
    is the default — only opt in by listing the domain)."""
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    assert fw.get_agent_always_popup_domains("agent_a") == []


def test_get_agent_always_popup_domains_normalizes_case(vault_home):
    """Mixed-case entries from hand-edited rules are lowercased — the
    eTLD+1 returned by resolve_etld1 is already lowercased on the
    caller side, so the comparison stays case-insensitive."""
    rules = {
        "agent_profiles": {
            "agent_a": {"web_auth": {
                "allowed_domains": ["yarcheplus.ru"],
                "always_popup": ["YarchePlus.RU", "Ozon.ru"],
            }}
        }
    }
    fw = _make_firewall(vault_home, rules)
    assert fw.get_agent_always_popup_domains("agent_a") == [
        "yarcheplus.ru", "ozon.ru",
    ]


def test_browse_page_always_popup_bypasses_headless(vault_home, fresh_cookies):
    """Domain on always_popup whitelist → browse_page skips _auth_browse_html
    entirely and calls _request_popup_fallback directly with reason
    'always_popup'."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {
        "agent_profiles": {
            "agent_a": {"web_auth": {
                "allowed_domains": ["yarcheplus.ru"],
                "always_popup": ["yarcheplus.ru"],
            }}
        }
    }
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "yarcheplus.ru", fresh_cookies)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx_with_firewall(agent_root, fw)

    # Sentinel: if _auth_browse_html runs the always_popup shortcut
    # failed to take effect.
    headless_called = {"value": False}
    original_html = browser_mod._auth_browse_html
    browser_mod._auth_browse_html = lambda *a, **kw: (
        headless_called.__setitem__("value", True),
        "<html><body>SHOULD NOT BE FETCHED</body></html>",
    )[1]

    # Sentinel: _request_popup_fallback must be called with reason='always_popup'.
    popup_captured = {"called": False}
    original_popup = browser_mod._request_popup_fallback

    async def _fake_popup(ctx, agent_id, domain, url, reason="anti_bot_challenge"):
        popup_captured["called"] = True
        popup_captured["agent_id"] = agent_id
        popup_captured["domain"] = domain
        popup_captured["url"] = url
        popup_captured["reason"] = reason
        return "<html><body><h1>YarchePlus orders rendered</h1></body></html>"

    browser_mod._request_popup_fallback = _fake_popup
    try:
        out = asyncio.run(
            browser_mod.browse_page(
                ctx,
                url="https://yarcheplus.ru/profile/orders/group/16659751",
                use_auth="yarcheplus.ru",
            )
        )
    finally:
        browser_mod._auth_browse_html = original_html
        browser_mod._request_popup_fallback = original_popup

    assert headless_called["value"] is False, (
        "always_popup hit should skip _auth_browse_html headless fetch"
    )
    assert popup_captured["called"] is True
    assert popup_captured["reason"] == "always_popup"
    assert popup_captured["url"].endswith("/16659751")
    assert "YarchePlus orders rendered" in out


def test_browse_page_always_popup_miss_falls_through_to_headless(
    vault_home, fresh_cookies
):
    """Domain NOT on always_popup whitelist → browse_page proceeds to
    the normal _auth_browse_html headless fetch (existing T9 happy path)."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {
        "agent_profiles": {
            "agent_a": {"web_auth": {
                "allowed_domains": ["ozon.ru"],
                "always_popup": ["yarcheplus.ru"],  # different domain
            }}
        }
    }
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", fresh_cookies)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx_with_firewall(agent_root, fw)

    headless_called = {"value": False}
    original_html = browser_mod._auth_browse_html

    def _sentinel(agent_id, domain, url):
        headless_called["value"] = True
        return "<html><body>ozon orders headless OK</body></html>"

    browser_mod._auth_browse_html = _sentinel
    try:
        out = asyncio.run(
            browser_mod.browse_page(
                ctx, url="https://ozon.ru/my/orders", use_auth="ozon.ru"
            )
        )
    finally:
        browser_mod._auth_browse_html = original_html

    assert headless_called["value"] is True, (
        "always_popup miss should reach the normal headless fetch"
    )
    assert "ozon orders headless OK" in out
