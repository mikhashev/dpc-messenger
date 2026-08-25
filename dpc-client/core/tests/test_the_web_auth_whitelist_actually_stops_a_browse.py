"""ADR-028 T5 promises a per-agent, per-domain gate before Camoufox opens.

Until 2026-08-25 that gate did not exist. `browse_page` contained the line
`firewall = None`, fetched `dpc_service` on the next line, and never took the
firewall off it: across the whole 249-line function the word «firewall»
appeared exactly twice — in a comment and in that assignment.
`get_agent_web_auth_domains` was never called and there was no denial branch.

Measured before the fix, with `allowed_domains: []` — deny everything — and
separately with an empty cookie vault: **both** launched Camoufox, fetched
311 238 characters of live HTML, and returned it to the agent described as
«auth domain wikipedia.org». The whitelist decided nothing; the only thing
between an agent and any site was whether cookies happened to be present — and
an empty vault turned out not to stop it either, which is a separate and
undecided question, kept out of this file on purpose.

`test_web_audit.py` caught this exactly as written and had been red, unread,
for some time. These tests exist so that the *reason* is pinned rather than
just the entry count — see the network assertion below, which is the part a
weaker fix would slip past.
"""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from .conftest import TEST_DOMAIN
# The vault fixture (temp DPC_HOME + in-memory keyring) lives beside the
# audit tests; importing it is how pytest shares a fixture across modules.
from .test_web_audit import vault_home  # noqa: F401


def _read_audit(home: Path, agent_id: str) -> list[dict]:
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fresh_cookies():
    return [{"name": "s", "value": "v", "domain": f".{TEST_DOMAIN}", "path": "/",
             "expires": int(time.time()) + 3600, "secure": True, "httponly": True,
             "samesite": "Lax"}]


def _firewall(home: Path, allowed: list[str]):
    from dpc_client_core.firewall import ContextFirewall

    rules_file = home / "privacy_rules.json"
    rules_file.write_text(json.dumps(
        {"agent_profiles": {"agent_a": {"web_auth": {"allowed_domains": allowed}}}}
    ), encoding="utf-8")
    return ContextFirewall(rules_file)


def _ctx(home: Path, firewall=None):
    root = home / "agents" / "agent_a"
    root.mkdir(parents=True, exist_ok=True)
    ctx = types.SimpleNamespace(agent_root=root)
    ctx.dpc_service = types.SimpleNamespace(firewall=firewall) if firewall else None
    return ctx


def _browse(ctx, **kw):
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    return asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=TEST_DOMAIN, **kw
    ))


# --- the whitelist -----------------------------------------------------------

def test_a_domain_outside_the_whitelist_is_refused(vault_home):
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())

    answer = _browse(_ctx(vault_home, _firewall(vault_home, [])))

    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == "firewall_denied:not_in_whitelist"
    assert "not in this agent's authorised web-auth domains" in answer


def test_the_refusal_happens_before_any_network_request(vault_home, monkeypatch):
    """The assertion that separates a real gate from a decorative one.

    A version that audits the denial and then browses anyway satisfies every
    count assertion in `test_web_audit.py`. It does not satisfy this one — and
    the old code's failure mode was exactly that shape: a `domain_blocked`
    audit row for a sub-resource, followed by a completed page fetch.
    """
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())

    opened: list[str] = []
    for name in ("_auth_browse_html", "_get_or_create_session_async",
                 "_get_or_create_fetch_session", "_browse_sync"):
        monkeypatch.setattr(
            browser_mod, name,
            lambda *a, _n=name, **kw: opened.append(_n) or (_ for _ in ()).throw(
                AssertionError(f"{_n} was called after a firewall denial")
            ),
        )

    _browse(_ctx(vault_home, _firewall(vault_home, [])))

    assert opened == [], f"a denied browse still reached: {opened}"


def test_a_whitelisted_domain_still_goes_through(vault_home, monkeypatch):
    """A gate that refuses everything is not a gate, it is an outage."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    _browse(_ctx(vault_home, _firewall(vault_home, [TEST_DOMAIN])))

    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == 200


def test_a_subdomain_of_a_whitelisted_domain_is_allowed(vault_home, monkeypatch):
    """The vault is keyed by eTLD+1, so the gate must be too — otherwise
    `www.example.com` is refused while its cookie jar sits under `example.com`."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    ctx = _ctx(vault_home, _firewall(vault_home, [TEST_DOMAIN]))
    asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://www.{TEST_DOMAIN}/my", use_auth=f"www.{TEST_DOMAIN}",
    ))

    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == 200


# --- what this gate does not decide -----------------------------------------

def test_an_empty_vault_is_left_to_the_existing_behaviour(vault_home, monkeypatch):
    """Deliberately not asserted here: whether `use_auth` with no cookies
    should refuse or browse on.

    ADR-028 specifies the whitelist and says nothing about the vault, and the
    suite contradicts itself — `test_web_audit.py::test_browse_page_audit_on_
    auth_required` expects a refusal, while `test_auth_browser.py`'s own
    docstring records the tolerant behaviour as intended («AuthBrowser.open()
    is tolerant of missing cookies… re-login surfaces only when a protected
    request is rejected»). I implemented the refusal, saw it redden eight tests
    written against the tolerant reading, and backed it out: that is a design
    decision, not a defect, and it belongs to whoever owns ADR-028.

    What this test pins is only that the whitelist gate does not accidentally
    settle it — a whitelisted domain with an empty vault still reaches the
    browse layer, exactly as before.
    """
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    reached: list = []
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: reached.append(a) or "<html><body><p>ok</p></body></html>",
    )

    _browse(_ctx(vault_home, _firewall(vault_home, [TEST_DOMAIN])))

    assert reached, "the whitelist gate must not have become a vault gate"


# --- the unit-test bypass, deliberately preserved ----------------------------

def test_without_a_wired_firewall_the_gate_is_skipped_by_design(vault_home, monkeypatch):
    """`firewall is None` is the pure-unit-test context, and the comment in
    `browse_page` has always said those tests bypass the gate deliberately.

    Worth pinning rather than leaving implicit: it means a production wiring
    mistake that leaves `dpc_service` unset restores the whole hole silently.
    The line is here so that a future decision to fail closed changes a test
    that says why, instead of one that merely goes red.
    """
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    reached: list = []
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: reached.append(a) or "<html><body><p>ok</p></body></html>",
    )

    _browse(_ctx(vault_home, firewall=None))

    assert reached, "no firewall wired: the whitelist is not consulted, by design"
