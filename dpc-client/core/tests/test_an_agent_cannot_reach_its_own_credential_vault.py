"""The web credential vault is out of reach of the agent it bounds.

`_vault_path` used to resolve under `~/.dpc/agents/{id}/`, which is the root
the agent's own file tools are sandboxed to: `read_file("web_credentials.enc")`
was a legal relative read, and the same name was a legal write and a legal
delete. What is pinned here is the property, not the spelling.

The sandbox root and the pre-move filename are spelled out locally rather than
asked of `web_auth`: they are what the *agent* sees, the old world's two fixed
points, and a test that asked the module for both would agree with whatever the
module did and fail to notice a move back. Where the vault lives *now* is the
one thing asked for, because that is what is allowed to change.
"""
from __future__ import annotations

import time

import pytest

from .conftest import TEST_DOMAIN


AGENT = "agent_a"
VAULT_FILENAME = "web_credentials.enc"


def _sandbox_root(home):
    """`~/.dpc/agents/{id}/` — the directory an agent may read and write by
    default (GLOSSARY: *sandbox*), and what `ToolContext.agent_root` is set
    to."""
    return home / "agents" / AGENT


@pytest.fixture
def vault_home(tmp_path, monkeypatch):
    """Isolated DPC_HOME plus an in-memory keyring, so nothing here touches
    the developer's real vault or DPAPI store."""
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
def session_cookies():
    return [
        {
            "name": "auth_token",
            "value": "the-one-real-session",
            "domain": f".{TEST_DOMAIN}",
            "path": "/",
            "expires": int(time.time()) + 86400,
            "secure": True,
            "httpOnly": True,
        },
    ]


def _sandbox_resolution(agent_root, name: str, require_write: bool = False):
    """Where the agent's own file tools send a relative path."""
    from dpc_client_core.dpc_agent.tools import core as core_tools
    from dpc_client_core.dpc_agent.tools.registry import ToolContext

    ctx = ToolContext(agent_root=agent_root)
    return core_tools._resolve_file_path(ctx, name, require_write=require_write)


def test_a_relative_sandbox_read_of_the_vault_filename_reaches_no_vault(
    vault_home, session_cookies
):
    """The headline property. A jar exists; the agent asks its own file tools
    for `web_credentials.enc`; what comes back must not be that jar."""
    from dpc_client_core import web_auth

    web_auth.save_cookies(AGENT, TEST_DOMAIN, session_cookies)
    agent_root = _sandbox_root(vault_home)
    agent_root.mkdir(parents=True, exist_ok=True)

    for require_write in (False, True):
        landed = _sandbox_resolution(
            agent_root, VAULT_FILENAME, require_write=require_write
        )
        assert not landed.exists(), (
            f"a relative sandbox path resolved onto an existing file at "
            f"{landed} — the vault is reachable from inside the sandbox"
        )

    # Not merely a different name under the same root: no relative path can
    # reach it, because it is not under agent_root at all.
    vault = web_auth._vault_path(AGENT)
    assert vault.exists(), "a saved jar has to land somewhere"
    with pytest.raises(ValueError):
        vault.resolve().relative_to(agent_root.resolve())


def test_the_vault_still_round_trips(vault_home, session_cookies):
    from dpc_client_core import web_auth

    assert web_auth.save_cookies(AGENT, TEST_DOMAIN, session_cookies) is True
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN) == session_cookies
    assert web_auth.has_session(AGENT, TEST_DOMAIN) is True
    assert [r["domain"] for r in web_auth.list_domains(AGENT)] == [TEST_DOMAIN]

    # Per-agent isolation survives the move out of the per-agent directory.
    assert web_auth.load_cookies("agent_b", TEST_DOMAIN) is None


def test_an_id_that_is_not_one_never_becomes_a_path(vault_home):
    """At node level a `..` in the id reaches the identity files."""
    from dpc_client_core import web_auth

    for bad in ("../node", "a/b", "", ".."):
        with pytest.raises(ValueError):
            web_auth._vault_path(bad)


def test_a_jar_left_in_the_sandbox_is_moved_out_and_read(
    vault_home, session_cookies
):
    """The migration, from the state an upgrade actually finds: a jar at the
    old in-sandbox path, written by the code that put it there."""
    from dpc_client_core import web_auth

    old = _sandbox_root(vault_home) / VAULT_FILENAME
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(_encrypted_jar(vault_home, session_cookies))
    assert old.exists()

    # A plain read is enough to move it.
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN) == session_cookies

    assert not old.exists(), "a copy left in the sandbox is the hole still open"
    assert web_auth._vault_path(AGENT).exists(), "the jar has to arrive somewhere"


def test_the_previous_generation_travels_with_the_jar(
    vault_home, session_cookies
):
    """`save_cookies` keeps one displaced generation inside the blob, so the
    move carries it — and `restore_previous_cookies` finds it after."""
    from dpc_client_core import web_auth

    older = [dict(session_cookies[0], value="the-jar-that-was-displaced")]
    old = _sandbox_root(vault_home) / VAULT_FILENAME
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(
        _encrypted_jar(vault_home, session_cookies, previous=older)
    )

    assert web_auth.restore_previous_cookies(AGENT, TEST_DOMAIN) is True
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN) == older
    assert not old.exists()


def test_a_failed_move_leaves_the_old_jar_alone(vault_home, session_cookies):
    """The old file is the only copy until the new one is in place, so a
    failure on the way must cost nothing.

    The fault gets a MonkeyPatch of its own rather than the test's: undoing
    the shared one would also undo `vault_home`'s DPC_HOME, and the rest of
    this test would then run against the developer's real vault.
    """
    from dpc_client_core import web_auth

    old = _sandbox_root(vault_home) / VAULT_FILENAME
    old.parent.mkdir(parents=True, exist_ok=True)
    blob = _encrypted_jar(vault_home, session_cookies)
    old.write_bytes(blob)

    def _boom(src, dst):
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(web_auth.os, "replace", _boom)
        with pytest.raises(OSError):
            web_auth.load_cookies(AGENT, TEST_DOMAIN)
        assert old.exists() and old.read_bytes() == blob

    # With the fault gone the next read completes the move.
    assert web_auth.load_cookies(AGENT, TEST_DOMAIN) == session_cookies
    assert not old.exists()


def test_the_audit_log_stays_where_the_agent_can_read_it(vault_home):
    """Moving the vault must not drag the audit log out with it — the agent
    is meant to be able to introspect its own history."""
    from dpc_client_core import web_auth

    web_auth.audit_append(AGENT, TEST_DOMAIN, f"https://{TEST_DOMAIN}/", 200)
    audit = web_auth.audit_path(AGENT)
    agent_root = _sandbox_root(vault_home)

    assert audit.exists()
    assert _sandbox_resolution(agent_root, "web_audit.jsonl") == audit.resolve()


def _encrypted_jar(home, cookies, previous=None) -> bytes:
    """A vault blob in the on-disk shape, built without reference to where the
    module currently puts it — this is what the old code left behind."""
    import json

    from cryptography.fernet import Fernet

    from dpc_client_core import web_auth

    entry = {
        "cookies": cookies,
        "authenticated_at": "2026-09-11T00:00:00Z",
        "last_used_at": "2026-09-11T00:00:00Z",
    }
    if previous is not None:
        entry["previous"] = {
            "cookies": previous,
            "authenticated_at": "2026-09-10T00:00:00Z",
            "last_used_at": "2026-09-10T00:00:00Z",
        }
    vault = {"domains": {TEST_DOMAIN: entry}}
    key = web_auth._get_or_create_key(AGENT)
    return Fernet(key).encrypt(
        json.dumps(vault, ensure_ascii=False).encode("utf-8")
    )
