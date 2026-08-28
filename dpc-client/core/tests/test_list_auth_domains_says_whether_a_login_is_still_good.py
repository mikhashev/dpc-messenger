"""`list_auth_domains` printed a raw epoch and called every jar authenticated.

Observed 2026-08-24 in agent_001's own tool calls:

    x.com: authenticated, cookies expire at unix=1785787087.833

That epoch is 2026-08-03 — three weeks before the call. The tool's own
description says it is the way to "check whether re-login is needed", and it
was the one thing the line could not tell you: a number in seconds since 1970
is not a fact an agent can act on, and "authenticated" was printed whether the
jar was fresh or long past its earliest expiry.

The claim stays narrow on purpose. `get_auth_status` returns the *earliest*
cookie expiry in the jar, so a past value means at least one cookie has
expired — not that the session is dead. The line has to say that and no more.
"""

import asyncio
import time

from dpc_client_core import web_auth
from dpc_client_core.dpc_agent.tools.web_auth_tools import list_auth_domains


class _Root:
    name = "agent_x"


class _Firewall:
    def __init__(self, domains):
        self._domains = domains

    def get_agent_web_auth_domains(self, agent_id):
        return self._domains


class _Service:
    def __init__(self, domains):
        self.firewall = _Firewall(domains)


class _Ctx:
    def __init__(self, domains):
        self.agent_root = _Root()
        self.dpc_service = _Service(domains)


def _run(monkeypatch, domains, statuses):
    monkeypatch.setattr(
        web_auth, "get_auth_status", lambda agent_id, d: statuses[d],
    )
    return asyncio.run(list_auth_domains(_Ctx(domains)))


def test_a_jar_past_its_earliest_expiry_says_so(monkeypatch):
    long_ago = 1785787087.833  # 2026-08-03, the value the live call printed
    out = _run(monkeypatch, ["x.com"],
               {"x.com": {"has_cookies": True, "expires": long_ago}})
    assert "expired" in out
    assert "2026-08-03" in out


def test_a_live_jar_says_when_it_runs_out(monkeypatch):
    later = time.time() + 86400 * 5
    out = _run(monkeypatch, ["ozon.ru"],
               {"ozon.ru": {"has_cookies": True, "expires": later}})
    assert "expires" in out
    assert "expired" not in out


def test_the_raw_epoch_is_not_what_the_agent_is_handed(monkeypatch):
    out = _run(monkeypatch, ["x.com"],
               {"x.com": {"has_cookies": True, "expires": 1785787087.833}})
    assert "unix=" not in out
    assert "1785787087" not in out


def test_it_does_not_claim_the_session_is_dead(monkeypatch):
    """The earliest cookie is not the session cookie. Saying "not logged in"
    here would be a claim wider than what `get_auth_status` measures."""
    out = _run(monkeypatch, ["x.com"],
               {"x.com": {"has_cookies": True, "expires": 1785787087.833}})
    assert "not logged in" not in out


def test_a_session_only_jar_is_unchanged(monkeypatch):
    out = _run(monkeypatch, ["y.ru"],
               {"y.ru": {"has_cookies": True, "expires": None}})
    assert "session-only cookies" in out


def test_an_empty_jar_is_unchanged(monkeypatch):
    out = _run(monkeypatch, ["z.ru"],
               {"z.ru": {"has_cookies": False, "expires": None}})
    assert "not logged in (re-login required)" in out
