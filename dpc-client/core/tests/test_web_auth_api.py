"""Tests for ADR-028 Wiring — CoreService web_auth_* WS handlers.

Exercise the 4 new commands without spinning up the full CoreService:
build a minimal stub holding only `firewall` + `local_api` (mocked
broadcast) and call the unbound CoreService coroutines on it.

  - web_auth_login_complete: persists cookies, broadcasts status_changed
  - web_auth_list_domains: returns whitelist + cookie status per domain
  - web_auth_add_domain: appends + saves + broadcasts; idempotent dup
  - web_auth_remove_domain: removes + revokes vault + broadcasts;
    no-op if not in whitelist; orphan cookies cleaned up
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


class _BroadcastRecorder:
    """Minimal local_api stub — records broadcast_event calls."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def broadcast_event(self, event_name: str, payload: dict):
        self.events.append((event_name, payload))


def _build_stub(tmp_path: Path, rules: dict):
    """Build the minimal stub that the 4 handlers need: firewall +
    local_api. The handlers are coroutines on CoreService — we bind
    them via __get__ at call time."""
    from dpc_client_core.firewall import ContextFirewall

    rules_file = tmp_path / "privacy_rules.json"
    rules_file.write_text(json.dumps(rules), encoding="utf-8")
    fw = ContextFirewall(rules_file)
    stub = types.SimpleNamespace()
    stub.firewall = fw
    stub.local_api = _BroadcastRecorder()
    return stub


def _call(stub, method_name: str, **kwargs):
    """Invoke an unbound CoreService coroutine method on the stub."""
    from dpc_client_core.service import CoreService

    coro = getattr(CoreService, method_name)
    return asyncio.run(coro(stub, **kwargs))


def _fresh_cookies():
    future = int(time.time()) + 3600
    return [{"name": "session_id", "value": "abc", "domain": ".ozon.ru",
             "path": "/", "expires": future, "secure": True,
             "httponly": True, "samesite": "Lax"}]


# ─────────────────────────────────────────────────────────────
# web_auth_login_complete
# ─────────────────────────────────────────────────────────────


def test_login_complete_persists_and_broadcasts(vault_home):
    from dpc_client_core import web_auth

    stub = _build_stub(vault_home, {})
    cookies = _fresh_cookies()
    result = _call(stub, "web_auth_login_complete",
                   agent_id="agent_a", domain="ozon.ru", cookies=cookies)
    assert result["status"] == "success"
    assert result["cookies_count"] == 1
    # Cookies actually landed in vault
    assert web_auth.load_cookies("agent_a", "ozon.ru") == cookies
    # Status broadcast fired
    assert ("web_auth_status_changed", {
        "agent_id": "agent_a", "domain": "ozon.ru", "has_cookies": True
    }) in stub.local_api.events


def test_login_complete_validates_inputs(vault_home):
    stub = _build_stub(vault_home, {})
    bad_cases = [
        {"agent_id": "", "domain": "ozon.ru", "cookies": []},
        {"agent_id": "agent_a", "domain": "", "cookies": []},
        {"agent_id": "agent_a", "domain": "ozon.ru", "cookies": "not-a-list"},
    ]
    for case in bad_cases:
        result = _call(stub, "web_auth_login_complete", **case)
        assert result["status"] == "error"


# ─────────────────────────────────────────────────────────────
# web_auth_list_domains
# ─────────────────────────────────────────────────────────────


def test_list_domains_empty_whitelist(vault_home):
    stub = _build_stub(vault_home, {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}})
    result = _call(stub, "web_auth_list_domains", agent_id="agent_a")
    assert result["status"] == "success"
    assert result["domains"] == []


def test_list_domains_returns_status_per_domain(vault_home):
    from dpc_client_core import web_auth

    stub = _build_stub(vault_home, {"agent_profiles": {
        "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru", "yarcheplus.ru"]}}
    }})
    web_auth.save_cookies("agent_a", "ozon.ru", _fresh_cookies())

    result = _call(stub, "web_auth_list_domains", agent_id="agent_a")
    assert result["status"] == "success"
    by_domain = {d["domain"]: d for d in result["domains"]}
    assert by_domain["ozon.ru"]["has_cookies"] is True
    assert by_domain["ozon.ru"]["expires"] is not None
    assert by_domain["yarcheplus.ru"]["has_cookies"] is False


def test_list_domains_per_agent_isolation(vault_home):
    stub = _build_stub(vault_home, {"agent_profiles": {
        "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}},
        "agent_b": {"web_auth": {"allowed_domains": ["yarcheplus.ru"]}},
    }})
    result_a = _call(stub, "web_auth_list_domains", agent_id="agent_a")
    domains_a = {d["domain"] for d in result_a["domains"]}
    assert domains_a == {"ozon.ru"}
    assert "yarcheplus.ru" not in domains_a


# ─────────────────────────────────────────────────────────────
# web_auth_add_domain
# ─────────────────────────────────────────────────────────────


def test_add_domain_appends_to_whitelist(vault_home):
    stub = _build_stub(vault_home, {})
    result = _call(stub, "web_auth_add_domain",
                   agent_id="agent_a", domain="ozon.ru")
    assert result["status"] == "success"
    # Round-trip via firewall confirms write
    assert "ozon.ru" in stub.firewall.get_agent_web_auth_domains("agent_a")
    # Broadcast fired
    assert ("web_auth_domains_changed", {
        "agent_id": "agent_a", "action": "added", "domain": "ozon.ru"
    }) in stub.local_api.events


def test_add_domain_normalizes_case_and_trims(vault_home):
    stub = _build_stub(vault_home, {})
    result = _call(stub, "web_auth_add_domain",
                   agent_id="agent_a", domain="  Ozon.RU  ")
    assert result["status"] == "success"
    assert result["domain"] == "ozon.ru"
    assert "ozon.ru" in stub.firewall.get_agent_web_auth_domains("agent_a")


def test_add_domain_duplicate_silent(vault_home):
    """Adding the same domain twice is a no-op rather than an error —
    matches the idempotent-write spirit of the firewall save path."""
    stub = _build_stub(vault_home, {})
    _call(stub, "web_auth_add_domain", agent_id="agent_a", domain="ozon.ru")
    stub.local_api.events.clear()
    result = _call(stub, "web_auth_add_domain",
                   agent_id="agent_a", domain="ozon.ru")
    assert result["status"] == "success"
    assert "already" in result.get("message", "").lower()
    # No duplicate broadcast
    assert not any(e[0] == "web_auth_domains_changed" for e in stub.local_api.events)


def test_add_domain_validates_inputs(vault_home):
    stub = _build_stub(vault_home, {})
    for bad_domain in ["", "   ", None]:
        result = _call(stub, "web_auth_add_domain",
                       agent_id="agent_a", domain=bad_domain)
        assert result["status"] == "error"


def test_add_domain_rejects_obviously_invalid_hostnames(vault_home):
    """Ark S140 [#82] review: UI input is user-typed, so reject things
    that can't be hostnames (no dot, contains spaces)."""
    stub = _build_stub(vault_home, {})
    for bad in ["not-a-domain", "ozon ru", "javascript:alert(1)", "single-token"]:
        result = _call(stub, "web_auth_add_domain",
                       agent_id="agent_a", domain=bad)
        assert result["status"] == "error", f"should reject {bad!r}"
        assert "hostname" in result["message"].lower()


def test_add_domain_normalizes_url_to_etld1(vault_home):
    """Mike S141 surface: user pastes `https://www.ozon.ru/` into the
    Web Authentication panel; the entry must land as `ozon.ru` so the
    read-side hostname compare in `firewall.is_authenticated` matches
    when the agent calls `browse_page(use_auth='ozon.ru')`.

    Pre-fix the literal URL string was stored and matching always failed.
    """
    cases = [
        ("https://www.ozon.ru/", "ozon.ru"),
        ("http://ozon.ru:8080/my/orders", "ozon.ru"),
        ("www.ozon.ru", "ozon.ru"),
        ("OZON.RU", "ozon.ru"),
        ("  Https://Login.Ozon.RU/path  ", "ozon.ru"),
    ]
    for raw, expected in cases:
        stub = _build_stub(vault_home, {})
        result = _call(stub, "web_auth_add_domain",
                       agent_id="agent_a", domain=raw)
        assert result["status"] == "success", f"{raw!r} → {result}"
        assert result["domain"] == expected, f"{raw!r} → {result}"
        assert expected in stub.firewall.get_agent_web_auth_domains("agent_a")


def test_add_domain_url_then_bare_is_duplicate(vault_home):
    """Adding `https://www.ozon.ru/` then `ozon.ru` (or vice versa)
    must be detected as the same entry — both normalize to the same
    eTLD+1 key. Without the fix this would silently create two
    entries pointing at the same logical site, only one of which
    matched at read time."""
    stub = _build_stub(vault_home, {})
    _call(stub, "web_auth_add_domain",
          agent_id="agent_a", domain="https://www.ozon.ru/")
    stub.local_api.events.clear()
    result = _call(stub, "web_auth_add_domain",
                   agent_id="agent_a", domain="ozon.ru")
    assert result["status"] == "success"
    assert "already" in result.get("message", "").lower()
    assert stub.firewall.get_agent_web_auth_domains("agent_a") == ["ozon.ru"]


def test_remove_domain_accepts_url_form(vault_home):
    """`web_auth_remove_domain` must accept the same URL forms as
    `add_domain` so a user pasting `https://www.ozon.ru/` into a
    remove confirmation hits the existing `ozon.ru` whitelist entry."""
    stub = _build_stub(vault_home, {"agent_profiles": {
        "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}
    }})
    result = _call(stub, "web_auth_remove_domain",
                   agent_id="agent_a", domain="https://www.ozon.ru/")
    assert result["status"] == "success"
    assert result["removed_from_whitelist"] is True
    assert stub.firewall.get_agent_web_auth_domains("agent_a") == []


# ─────────────────────────────────────────────────────────────
# web_auth_remove_domain
# ─────────────────────────────────────────────────────────────


def test_remove_domain_strips_and_revokes_cookies(vault_home):
    """Removing from whitelist MUST also revoke vault cookies — keeping
    orphan cookies after de-authorization would be a privacy bug (a
    later re-add would silently re-authorize on stale cookies)."""
    from dpc_client_core import web_auth

    stub = _build_stub(vault_home, {"agent_profiles": {
        "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}
    }})
    web_auth.save_cookies("agent_a", "ozon.ru", _fresh_cookies())
    assert web_auth.load_cookies("agent_a", "ozon.ru") is not None

    result = _call(stub, "web_auth_remove_domain",
                   agent_id="agent_a", domain="ozon.ru")
    assert result["status"] == "success"
    assert result["removed_from_whitelist"] is True
    # Whitelist updated
    assert "ozon.ru" not in stub.firewall.get_agent_web_auth_domains("agent_a")
    # Cookies revoked
    assert web_auth.load_cookies("agent_a", "ozon.ru") is None
    # Broadcast fired
    assert ("web_auth_domains_changed", {
        "agent_id": "agent_a", "action": "removed", "domain": "ozon.ru"
    }) in stub.local_api.events


def test_remove_domain_not_in_whitelist_noop(vault_home):
    """Removing a domain that's not in the whitelist returns success
    with removed_from_whitelist=False and emits no broadcast."""
    stub = _build_stub(vault_home, {"agent_profiles": {
        "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}
    }})
    result = _call(stub, "web_auth_remove_domain",
                   agent_id="agent_a", domain="yandex.ru")
    assert result["status"] == "success"
    assert result["removed_from_whitelist"] is False
    assert not any(e[0] == "web_auth_domains_changed" for e in stub.local_api.events)
