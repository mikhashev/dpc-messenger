"""Tests for ADR-028 T5: firewall.is_auth_domain_allowed (per-agent + per-domain gate).

Covers the firewall layer that gates `browse_page(use_auth=...)`:

  - Empty / missing whitelist → denied
  - Domain not in whitelist → denied
  - Whitelist OK but vault empty → denied
  - Whitelist OK + cookies present + not expired → allowed
  - Whitelist OK + cookies present + expired → denied
  - Per-agent isolation (agent_a allowed, agent_b denied)
  - eTLD+1 normalization (whitelist f'{TEST_DOMAIN}' admits f'www.{TEST_DOMAIN}')
  - browse_page integration: firewall denial returns ⚠️ before browser launch
"""
from __future__ import annotations

from .conftest import TEST_DOMAIN, TEST_DOMAIN_WWW, TEST_DOMAIN_URL
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
        {"name": "session_id", "value": "abc", "domain": f".{TEST_DOMAIN}",
         "path": "/", "expires": future, "secure": True,
         "httponly": True, "samesite": "Lax"},
    ]


@pytest.fixture
def expired_cookies():
    past = int(time.time()) - 3600
    return [
        {"name": "session_id", "value": "abc", "domain": f".{TEST_DOMAIN}",
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
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is False


def test_empty_whitelist_denies(vault_home):
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}}
    fw = _make_firewall(vault_home, rules)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is False


def test_domain_not_in_whitelist_denies(vault_home, fresh_cookies):
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "yandex.ru", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", "yandex.ru") is False


# ─────────────────────────────────────────────────────────────
# Cookie presence + expiry
# ─────────────────────────────────────────────────────────────


def test_whitelisted_but_no_cookies_denies(vault_home):
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is False


def test_whitelisted_and_fresh_cookies_allows(vault_home, fresh_cookies):
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is True


def test_whitelisted_but_expired_cookies_denies(vault_home, expired_cookies):
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", expired_cookies)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is False


# ─────────────────────────────────────────────────────────────
# Per-agent isolation (Mike S140 [#54] requirement)
# ─────────────────────────────────────────────────────────────


def test_per_agent_isolation(vault_home, fresh_cookies):
    """agent_a is authorized for ozon.ru, agent_b is not. Both have
    cookies in their vaults — only agent_a passes the firewall."""
    from dpc_client_core import web_auth

    rules = {
        "agent_profiles": {
            "agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}},
            "agent_b": {"web_auth": {"allowed_domains": []}},
        }
    }
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    web_auth.save_cookies("agent_b", f"{TEST_DOMAIN}", fresh_cookies)

    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is True
    assert fw.is_auth_domain_allowed("agent_b", f"{TEST_DOMAIN}") is False


# ─────────────────────────────────────────────────────────────
# eTLD+1 normalization (subdomain accepted via whitelist root)
# ─────────────────────────────────────────────────────────────


def test_whitelist_case_insensitive(vault_home, fresh_cookies):
    """Hand-edited privacy_rules.json may have mixed-case entries like
    'Ozon.RU'. The whitelist comparison must be case-insensitive — the
    requested domain is already lowercased by resolve_etld1, the
    whitelist is now also normalized before the `in` check."""
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [TEST_DOMAIN.title()]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is True
    assert fw.is_auth_domain_allowed("agent_a", f"www.{TEST_DOMAIN}") is True


def test_subdomain_admitted_via_etld1(vault_home, fresh_cookies):
    """Whitelist contains the eTLD+1 f'{TEST_DOMAIN}'. Requesting f'www.{TEST_DOMAIN}'
    or f'login.{TEST_DOMAIN}' should resolve to the same eTLD+1 and pass."""
    from dpc_client_core import web_auth

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    # Cookies saved under the root domain (T3 already resolves eTLD+1)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)

    assert fw.is_auth_domain_allowed("agent_a", f"www.{TEST_DOMAIN}") is True
    assert fw.is_auth_domain_allowed("agent_a", f"login.{TEST_DOMAIN}") is True


# ─────────────────────────────────────────────────────────────
# T9 (S142) — denial reason diagnostic (per-cause UX messages)
# ─────────────────────────────────────────────────────────────


def test_denial_reason_not_in_whitelist_empty(vault_home):
    """Empty allowed_domains → 'not_in_whitelist' regardless of cookies."""
    fw = _make_firewall(
        vault_home,
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}},
    )
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") == "not_in_whitelist"


def test_denial_reason_not_in_whitelist_other_domain(vault_home, fresh_cookies):
    """Domain not in (non-empty) whitelist → 'not_in_whitelist' even with
    cookies in the vault."""
    from dpc_client_core import web_auth

    fw = _make_firewall(
        vault_home,
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}},
    )
    web_auth.save_cookies("agent_a", "yandex.ru", fresh_cookies)
    assert fw.get_auth_denial_reason("agent_a", "yandex.ru") == "not_in_whitelist"


def test_denial_reason_cookies_missing(vault_home):
    """Domain whitelisted but no cookies in vault → 'cookies_missing' —
    points the user at the Login button instead of the whitelist."""
    fw = _make_firewall(
        vault_home,
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}},
    )
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") == "cookies_missing"


def test_denial_reason_cookies_expired(vault_home, expired_cookies):
    """Domain whitelisted, cookies present but past expiry →
    'cookies_expired' — points the user at Re-login, not the whitelist
    (the Mike S142 [#49] case that motivated this method)."""
    from dpc_client_core import web_auth

    fw = _make_firewall(
        vault_home,
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}},
    )
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", expired_cookies)
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") == "cookies_expired"


def test_denial_reason_none_when_allowed(vault_home, fresh_cookies):
    """Happy path: whitelist match + fresh cookies → None (no denial)."""
    from dpc_client_core import web_auth

    fw = _make_firewall(
        vault_home,
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}},
    )
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") is None


def test_denial_reason_matches_is_auth_domain_allowed(vault_home, fresh_cookies, expired_cookies):
    """Invariant: get_auth_denial_reason returns None iff
    is_auth_domain_allowed returns True. The two methods MUST agree —
    otherwise the UI advice will mislead."""
    from dpc_client_core import web_auth

    fw = _make_firewall(
        vault_home,
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}},
    )
    # Case 1: empty vault — both reject
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is False
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") is not None
    # Case 2: fresh cookies — both accept
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is True
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") is None
    # Case 3: replace with expired cookies — both reject
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", expired_cookies)
    assert fw.is_auth_domain_allowed("agent_a", f"{TEST_DOMAIN}") is False
    assert fw.get_auth_denial_reason("agent_a", f"{TEST_DOMAIN}") == "cookies_expired"


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
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)

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
                ctx, url=f"https://{TEST_DOMAIN}/my/orders", use_auth=f"{TEST_DOMAIN}"
            )
        )
    finally:
        browser_mod._auth_browse_html = original

    assert called["value"] is False, "firewall failed to block _auth_browse_html"
    assert out.startswith("⚠️")
    assert "agent_a" in out
    assert "allowed_domains" in out


def test_browse_page_denial_message_says_relogin_for_expired_cookies(
    vault_home, expired_cookies
):
    """S142 [#49] regression: when blocked by expired cookies, the
    user-facing message must say 'Re-login' (not 'add to allowed_domains'
    — the domain IS in the whitelist, the problem is the vault). Mike
    was misled by the old generic 'not authorized' phrasing into
    thinking the whitelist needed editing."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", expired_cookies)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx_with_firewall(agent_root, fw)

    out = asyncio.run(
        browser_mod.browse_page(
            ctx, url=f"https://{TEST_DOMAIN}/my/orders", use_auth=f"{TEST_DOMAIN}"
        )
    )

    assert out.startswith("⚠️")
    assert "expired" in out.lower()
    assert "re-login" in out.lower()
    # And the message MUST NOT redirect the user to the whitelist —
    # that's what made Mike edit allowed_domains when he didn't need to.
    assert "allowed_domains" not in out


def test_browse_page_denial_message_says_login_for_missing_cookies(vault_home):
    """Domain whitelisted but no cookies in vault → message points at
    'Login' (first-time auth), not 'Re-login' (refresh expired)."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx_with_firewall(agent_root, fw)

    out = asyncio.run(
        browser_mod.browse_page(
            ctx, url=f"https://{TEST_DOMAIN}/my/orders", use_auth=f"{TEST_DOMAIN}"
        )
    )

    assert out.startswith("⚠️")
    assert "no saved login" in out.lower()
    assert "click 'login'" in out.lower()


def test_browse_page_firewall_pass_proceeds_to_auth_browse(vault_home, fresh_cookies):
    """browse_page(use_auth=...) with firewall passing reaches the
    authenticated browse path (_auth_browse_html after T9 split)."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", fresh_cookies)

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
                ctx, url=f"https://{TEST_DOMAIN}/my/orders", use_auth=f"{TEST_DOMAIN}"
            )
        )
    finally:
        browser_mod._auth_browse_html = original

    assert captured["called"] is True
    assert captured["agent_id"] == "agent_a"
    assert captured["domain"] == f"{TEST_DOMAIN}"
    assert "stub-page-content" in out
