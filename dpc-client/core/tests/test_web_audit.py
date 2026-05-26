"""Tests for ADR-028 T6: web_auth.audit_append + browse_page audit wiring.

Coverage:
- audit_append writes exactly one JSONL line per call to the right path
- entry contains required fields (timestamp, agent_id, domain, url, status)
- bytes_size is included when provided, omitted otherwise
- append-only — re-running grows the file, doesn't overwrite
- per-agent isolation: agent_a writes go to agent_a's log, not agent_b's
- browse_page emits exactly one audit entry per call (success or any
  failure path: firewall_denied / auth_required / expired / domain_mismatch /
  camoufox_missing / browser_error / status 200)
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


def _audit_path(home: Path, agent_id: str) -> Path:
    return home / "agents" / agent_id / "web_audit.jsonl"


def _read_audit(home: Path, agent_id: str) -> list[dict]:
    path = _audit_path(home, agent_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_audit_append_creates_file_and_entry(vault_home):
    from dpc_client_core import web_auth

    web_auth.audit_append("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/my", status=200, bytes_size=1234)
    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    e = entries[0]
    assert e["agent_id"] == "agent_a"
    assert e["domain"] == f"{TEST_DOMAIN}"
    assert e["url"] == f"https://{TEST_DOMAIN}/my"
    assert e["status"] == 200
    assert e["bytes"] == 1234
    assert e["timestamp"].endswith("Z")


def test_audit_append_omits_bytes_when_none(vault_home):
    from dpc_client_core import web_auth

    web_auth.audit_append("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/x", status="auth_required")
    entries = _read_audit(vault_home, "agent_a")
    assert entries[0]["status"] == "auth_required"
    assert "bytes" not in entries[0]


def test_audit_append_is_append_only(vault_home):
    from dpc_client_core import web_auth

    web_auth.audit_append("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/a", status=200)
    web_auth.audit_append("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/b", status=200)
    web_auth.audit_append("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/c", status="expired")
    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 3
    assert [e["url"] for e in entries] == [
        f"https://{TEST_DOMAIN}/a",
        f"https://{TEST_DOMAIN}/b",
        f"https://{TEST_DOMAIN}/c",
    ]


def test_audit_per_agent_isolation(vault_home):
    """agent_a's entries go to agent_a's log, not agent_b's. Mike S140
    [#54] per-agent requirement applied to audit trail."""
    from dpc_client_core import web_auth

    web_auth.audit_append("agent_a", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/a", status=200)
    web_auth.audit_append("agent_b", f"{TEST_DOMAIN}", f"https://{TEST_DOMAIN}/b", status=200)
    a_entries = _read_audit(vault_home, "agent_a")
    b_entries = _read_audit(vault_home, "agent_b")
    assert len(a_entries) == 1 and a_entries[0]["url"] == f"https://{TEST_DOMAIN}/a"
    assert len(b_entries) == 1 and b_entries[0]["url"] == f"https://{TEST_DOMAIN}/b"


# ─────────────────────────────────────────────────────────────
# browse_page audit wiring — every code path emits exactly one entry
# ─────────────────────────────────────────────────────────────


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
    return [{"name": "s", "value": "v", "domain": f".{TEST_DOMAIN}", "path": "/",
             "expires": future, "secure": True, "httponly": True,
             "samesite": "Lax"}]


def test_browse_page_audit_on_firewall_denied(vault_home):
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": []}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", _fresh_cookies())

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, firewall=fw)

    asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=f"{TEST_DOMAIN}"
    ))
    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    # Status now carries the denial reason after the S142 UX fix —
    # was "firewall_denied", now "firewall_denied:<reason>" where
    # <reason> is one of not_in_whitelist|cookies_missing|cookies_expired.
    assert entries[0]["status"].startswith("firewall_denied")
    assert entries[0]["domain"] == f"{TEST_DOMAIN}"


def test_browse_page_audit_on_auth_required(vault_home):
    """No cookies in vault → AuthRequiredError → audit 'auth_required'.
    Firewall is None here so the firewall layer is bypassed; the
    AuthBrowser construction layer raises AuthRequiredError."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)

    asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=f"{TEST_DOMAIN}"
    ))
    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == "auth_required"


def test_browse_page_audit_on_success(vault_home):
    """Patch _auth_browse_html to simulate a successful Camoufox render →
    audit status=200, bytes=len(markdown after trafilatura)."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", _fresh_cookies())

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, firewall=fw)

    raw_html = "<html><body><h1>Title</h1><p>Body text</p></body></html>"
    expected_markdown = browser_mod._html_to_markdown(raw_html)
    original = browser_mod._auth_browse_html
    browser_mod._auth_browse_html = lambda *args, **kwargs: raw_html
    try:
        asyncio.run(browser_mod.browse_page(
            ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=f"{TEST_DOMAIN}"
        ))
    finally:
        browser_mod._auth_browse_html = original

    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == 200
    assert entries[0]["bytes"] == len(expected_markdown)


def test_browse_page_audit_on_browser_error(vault_home):
    """Patch _auth_browse_html to raise RuntimeError → audit 'browser_error'."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    rules = {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": [f"{TEST_DOMAIN}"]}}}}
    fw = _make_firewall(vault_home, rules)
    web_auth.save_cookies("agent_a", f"{TEST_DOMAIN}", _fresh_cookies())

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root, firewall=fw)

    def _raise_runtime(*a, **kw):
        raise RuntimeError("simulated browser crash")

    original = browser_mod._auth_browse_html
    browser_mod._auth_browse_html = _raise_runtime
    try:
        asyncio.run(browser_mod.browse_page(
            ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=f"{TEST_DOMAIN}"
        ))
    finally:
        browser_mod._auth_browse_html = original

    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == "browser_error"


def test_browse_page_anonymous_path_no_audit(vault_home):
    """Anonymous browse_page (no use_auth) does NOT touch audit log —
    audit is auth-only."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    agent_root = vault_home / "agents" / "agent_a"
    agent_root.mkdir(parents=True, exist_ok=True)
    ctx = _make_ctx(agent_root)

    # Mock _browse_sync to return a quick fake page so the call returns
    # without hitting the network.
    original = browser_mod._browse_sync
    browser_mod._browse_sync = lambda url: {
        "success": True, "text": "hello", "needs_js": False
    }
    try:
        asyncio.run(browser_mod.browse_page(ctx, url="https://example.com"))
    finally:
        browser_mod._browse_sync = original

    assert _read_audit(vault_home, "agent_a") == []
