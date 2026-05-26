"""Tests for ADR-028 T3 credential vault (`web_auth.py`).

Covers:
- save / load round-trip preserves cookies exactly
- per-agent isolation (agent_b cannot read agent_a's vault)
- eTLD+1 resolution (subdomain → root jar)
- get_auth_status fields (has_cookies / expires / authenticated_at)
- revoke removes the jar
- is_expired correctness (session vs expired vs valid)
- vault file is not readable as plaintext
"""
from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    """Isolated DPC_HOME per test so vaults don't leak between cases.

    Also stubs the OS keyring with an in-memory dict so tests don't
    touch the user's real DPAPI/Keychain store.
    """
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
def sample_cookies():
    future = int(time.time()) + 3600
    return [
        {
            "name": "session_id",
            "value": "abc123",
            "domain": ".ozon.ru",
            "path": "/",
            "expires": future,
            "secure": True,
            "httponly": True,
            "samesite": "Lax",
        },
        {
            "name": "csrf",
            "value": "xyz789",
            "domain": "ozon.ru",
            "path": "/",
            "expires": None,
            "secure": True,
            "httponly": False,
            "samesite": "Strict",
        },
    ]


def test_round_trip_preserves_cookies(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    loaded = web_auth.load_cookies("agent_a", "ozon.ru")
    assert loaded == sample_cookies


def test_load_missing_returns_none(vault_home):
    from dpc_client_core import web_auth

    assert web_auth.load_cookies("agent_a", "unknown.com") is None


def test_per_agent_isolation(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    assert web_auth.load_cookies("agent_b", "ozon.ru") is None
    assert web_auth.list_domains("agent_b") == []


def test_etld1_resolution_subdomain(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    assert web_auth.load_cookies("agent_a", "www.ozon.ru") == sample_cookies
    assert web_auth.load_cookies("agent_a", "login.ozon.ru") == sample_cookies


def test_etld1_resolution_unknown_domain_passthrough(vault_home, sample_cookies):
    """An unknown domain should map to itself (no eTLD+1 entry exists)."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "unmapped.example", sample_cookies)
    assert web_auth.load_cookies("agent_a", "unmapped.example") == sample_cookies


def test_resolve_etld1_accepts_full_urls():
    """User pastes a URL in the UI; resolve_etld1 must strip scheme,
    port, path and still arrive at the same vault key.

    Mike S141 surface: agent sees `failed to add domain` for
    `https://www.ozon.ru/` because pre-fix the literal URL string was
    being stored and read-side hostname compare never matched.
    """
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1("https://www.ozon.ru/") == "ozon.ru"
    assert web_auth.resolve_etld1("http://ozon.ru:8080/my/orders") == "ozon.ru"
    assert web_auth.resolve_etld1("www.ozon.ru") == "ozon.ru"
    assert web_auth.resolve_etld1("ozon.ru") == "ozon.ru"
    assert web_auth.resolve_etld1("OZON.RU") == "ozon.ru"
    # Unmapped host but valid URL — eTLD+1 unknown so the bare
    # hostname is the vault key.
    assert web_auth.resolve_etld1("https://example.com/path") == "example.com"


def test_resolve_etld1_empty_or_hostnameless_input():
    """Inputs that yield no hostname after parsing → empty string so
    callers can reject. `javascript:alert(1)` is NOT in this bucket —
    urlsplit treats `javascript` as the host-like part of the authority
    when no `//` is present; the downstream dot-check (`add_domain`
    requires a `.` in the result) is what rejects it as a real domain.
    """
    from dpc_client_core import web_auth

    assert web_auth.resolve_etld1("") == ""
    assert web_auth.resolve_etld1("   ") == ""
    # scheme-only URLs: urlsplit returns no hostname.
    assert web_auth.resolve_etld1("http://") == ""
    assert web_auth.resolve_etld1("https://") == ""
    # Bare token with no dot: parses to a hostname-like string but the
    # add_domain caller rejects via dot-check, not here.
    assert web_auth.resolve_etld1("javascript:alert(1)") == "javascript"
    assert web_auth.resolve_etld1("localhost") == "localhost"


def test_etld1_resolution_via_url_reaches_existing_jar(vault_home, sample_cookies):
    """Read-side: agent calling load_cookies with a full URL hits the
    same jar written under the eTLD+1 key. This is the bug Mike caught
    end-to-end: cookies were unreachable if URL ever entered the lookup."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    assert web_auth.load_cookies("agent_a", "https://www.ozon.ru/orders") == sample_cookies
    assert web_auth.load_cookies("agent_a", "http://login.ozon.ru:443") == sample_cookies


def test_get_auth_status_empty(vault_home):
    from dpc_client_core import web_auth

    status = web_auth.get_auth_status("agent_a", "ozon.ru")
    assert status == {"has_cookies": False, "expires": None, "authenticated_at": None}


def test_get_auth_status_populated(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    status = web_auth.get_auth_status("agent_a", "ozon.ru")
    assert status["has_cookies"] is True
    assert status["expires"] == sample_cookies[0]["expires"]
    assert status["authenticated_at"] is not None


def test_list_domains(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    web_auth.save_cookies("agent_a", "yarcheplus.ru", sample_cookies)
    domains = web_auth.list_domains("agent_a")
    domain_names = {d["domain"] for d in domains}
    assert domain_names == {"ozon.ru", "yarcheplus.ru"}
    for entry in domains:
        assert entry["has_cookies"] is True
        assert entry["authenticated_at"] is not None


def test_revoke_removes_jar(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    web_auth.revoke("agent_a", "ozon.ru")
    assert web_auth.load_cookies("agent_a", "ozon.ru") is None


def test_revoke_missing_silent(vault_home):
    from dpc_client_core import web_auth

    web_auth.revoke("agent_a", "never_saved.com")  # must not raise


def test_is_expired_with_expired_cookie():
    from dpc_client_core import web_auth

    past = int(time.time()) - 100
    cookies = [{"name": "x", "value": "y", "expires": past}]
    assert web_auth.is_expired(cookies) is True


def test_is_expired_with_session_only():
    from dpc_client_core import web_auth

    cookies = [{"name": "x", "value": "y", "expires": None}]
    assert web_auth.is_expired(cookies) is False


def test_is_expired_with_future_cookie():
    from dpc_client_core import web_auth

    future = int(time.time()) + 3600
    cookies = [{"name": "x", "value": "y", "expires": future}]
    assert web_auth.is_expired(cookies) is False


def test_is_expired_ignores_playwright_session_marker():
    from dpc_client_core import web_auth

    cookies = [{"name": "lang", "value": "en", "expires": -1}]
    assert web_auth.is_expired(cookies) is False


def test_get_auth_status_ignores_playwright_session_marker(vault_home):
    from dpc_client_core import web_auth

    future = int(time.time()) + 3600
    web_auth.save_cookies("agent_a", "ozon.ru", [
        {"name": "lang", "value": "en", "domain": ".ozon.ru", "expires": -1},
        {"name": "auth", "value": "t", "domain": ".ozon.ru", "expires": future},
    ])
    status = web_auth.get_auth_status("agent_a", "ozon.ru")
    assert status["has_cookies"] is True
    assert status["expires"] == future


def test_invalid_token_recovery(vault_home, sample_cookies):
    """If the keyring entry is wiped (or rotated) while the on-disk
    vault still exists, the next load should NOT crash with
    InvalidToken. Instead the module should start fresh: a new key is
    generated, the un-decryptable blob is treated as empty, and the
    next save overwrites it."""
    from dpc_client_core import web_auth
    import keyring

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    # Wipe the keyring entry — on next access a new key will be generated
    # but the on-disk vault was encrypted with the old key.
    keyring.delete_password(web_auth.SERVICE, "agent_a")
    # load_cookies must not raise. Stale vault is silently dropped.
    assert web_auth.load_cookies("agent_a", "ozon.ru") is None
    # A subsequent save with the new key works end-to-end.
    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    assert web_auth.load_cookies("agent_a", "ozon.ru") == sample_cookies


def test_vault_file_not_plaintext(vault_home, sample_cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "ozon.ru", sample_cookies)
    vault_file = vault_home / "agents" / "agent_a" / "web_credentials.enc"
    assert vault_file.exists()
    raw = vault_file.read_bytes()
    # plaintext markers from sample_cookies must not appear in ciphertext
    assert b"session_id" not in raw
    assert b"abc123" not in raw
    assert b".ozon.ru" not in raw
    # cannot be parsed as JSON either
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
