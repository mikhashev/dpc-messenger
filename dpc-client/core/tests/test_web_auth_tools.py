"""Tests for ADR-028 T7: list_auth_domains agent tool.

Coverage:
- Empty whitelist → helpful "no domains configured" message
- Whitelist with no cookies → "not logged in" status per domain
- Whitelist with fresh cookies → "authenticated" status + expiry
- Per-agent isolation: agent_a's tool call returns only agent_a's domains
- Missing firewall (config error) → returns ⚠️ message
- get_tools() exports the tool with correct schema
- Tool is registered in firewall all_tools_defaults as enabled
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


def _make_firewall(tmp_path: Path, rules: dict):
    from dpc_client_core.firewall import ContextFirewall

    rules_file = tmp_path / "privacy_rules.json"
    rules_file.write_text(json.dumps(rules), encoding="utf-8")
    return ContextFirewall(rules_file)


def _make_ctx(agent_root: Path, firewall=None):
    ctx = types.SimpleNamespace()
    ctx.agent_root = agent_root
    if firewall is not None:
        ctx.dpc_service = types.SimpleNamespace()
        ctx.dpc_service.firewall = firewall
    else:
        ctx.dpc_service = None
    return ctx


def _fresh_cookies():
    future = int(time.time()) + 3600
    return [{"name": "s", "value": "v", "domain": ".ozon.ru", "path": "/",
             "expires": future, "secure": True, "httponly": True,
             "samesite": "Lax"}]


# ─────────────────────────────────────────────────────────────
# list_auth_domains output shape
# ─────────────────────────────────────────────────────────────


def test_no_domains_configured_returns_helpful_message(vault_home):
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}}
    fw = _make_firewall(vault_home, rules)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, fw)

    out = asyncio.run(list_auth_domains(ctx))
    assert "No web auth domains configured" in out
    assert "agent_a" in out
    assert "web_auth.allowed_domains" in out


def test_whitelist_no_cookies_returns_not_logged_in(vault_home):
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru", "yarcheplus.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, fw)

    out = asyncio.run(list_auth_domains(ctx))
    assert "ozon.ru" in out
    assert "yarcheplus.ru" in out
    assert out.count("not logged in") == 2


def test_whitelist_with_cookies_returns_authenticated_status(vault_home):
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru", "yarcheplus.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", _fresh_cookies())

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, fw)

    out = asyncio.run(list_auth_domains(ctx))
    # ozon.ru has cookies → authenticated
    assert "ozon.ru: authenticated" in out
    # yarcheplus.ru in whitelist but no cookies → not logged in
    assert "yarcheplus.ru: not logged in" in out


def test_session_only_cookies_reported_as_session(vault_home):
    """Cookies with expires=None should be reported as 'session-only'
    rather than displaying a numeric expiry."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    session_cookies = [{
        "name": "s", "value": "v", "domain": ".ozon.ru", "path": "/",
        "expires": None, "secure": True, "httponly": True, "samesite": "Lax"
    }]
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", "ozon.ru", session_cookies)

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, fw)

    out = asyncio.run(list_auth_domains(ctx))
    assert "session-only cookies" in out


# ─────────────────────────────────────────────────────────────
# Per-agent isolation (Mike S140 [#54] requirement)
# ─────────────────────────────────────────────────────────────


def test_per_agent_isolation(vault_home):
    """agent_a calling list_auth_domains sees only agent_a's whitelist;
    agent_b's domains are not exposed even though they're in the same
    privacy_rules.json file."""
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    rules = {"agent_profiles": {
        "agent_a": {"web_auth": {"allowed_domains": ["ozon.ru"]}},
        "agent_b": {"web_auth": {"allowed_domains": ["yarcheplus.ru"]}},
    }}
    fw = _make_firewall(vault_home, rules)

    agent_root_a = vault_home / "agents" / "agent_a"
    agent_root_a.mkdir(parents=True, exist_ok=True)
    ctx_a = _make_ctx(agent_root_a, fw)

    out_a = asyncio.run(list_auth_domains(ctx_a))
    assert "ozon.ru" in out_a
    assert "yarcheplus.ru" not in out_a, "agent_a leaked agent_b's whitelist"


# ─────────────────────────────────────────────────────────────
# Missing firewall (DPC core misconfiguration)
# ─────────────────────────────────────────────────────────────


def test_missing_firewall_returns_warning(vault_home):
    """If ctx.dpc_service is None or has no firewall attribute, the
    tool returns a clear ⚠️ message rather than crashing."""
    from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, firewall=None)

    out = asyncio.run(list_auth_domains(ctx))
    assert out.startswith("⚠️")
    assert "Firewall not available" in out


# ─────────────────────────────────────────────────────────────
# Tool registration
# ─────────────────────────────────────────────────────────────


def test_get_tools_exports_list_auth_domains():
    from dpc_client_core.dpc_agent.tools.web_auth_tools import get_tools

    tools = get_tools()
    assert len(tools) == 1
    entry = tools[0]
    assert entry.name == "list_auth_domains"
    assert entry.schema["name"] == "list_auth_domains"
    assert entry.schema["parameters"]["properties"] == {}
    assert entry.timeout_sec == 5
    assert callable(entry.handler)


def test_list_auth_domains_in_firewall_defaults(vault_home):
    """The tool must be registered in firewall.all_tools_defaults so
    new agents have it enabled by default. We verify by reading the
    parsed dpc_agent_tools dict after firewall load."""
    fw = _make_firewall(vault_home, {})
    assert "list_auth_domains" in fw.dpc_agent_tools
    assert fw.dpc_agent_tools["list_auth_domains"] is True


def test_get_agent_web_auth_domains_normalizes_case(vault_home):
    """firewall.get_agent_web_auth_domains lowercases whitelist entries,
    matching the existing case-insensitive lookup contract from T5."""
    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": ["Ozon.RU", "YARCHEPLUS.ru"]}}}}
    fw = _make_firewall(vault_home, rules)
    domains = fw.get_agent_web_auth_domains("agent_a")
    assert domains == ["ozon.ru", "yarcheplus.ru"]


def test_get_agent_web_auth_domains_empty_when_no_profile(vault_home):
    fw = _make_firewall(vault_home, {})
    assert fw.get_agent_web_auth_domains("agent_a") == []
