"""The gate is a recorded human approval, not the presence of a cookie jar.

Successor to `test_the_vault_is_the_web_auth_gate.py`, whose premise was
measured false: `save_cookies` stamped the jar on every write, and its
writers are the cookie writeback after each navigate and again on close.
agent_001's `x.com` jar read `authenticated_at 2026-09-10T17:49:02Z` with
the audit row at that same second being `close | x.com | ok` — the agent
was minting its own authorisation, so "jar present" mirrored the browser's
own work rather than gating it.

What replaces it, pinned here:
- `browse_page(use_auth=S)` spends a stored login and needs the approval,
  headed and headless alike — `keep_open=True` is no longer exempt, because
  a visible window does not make the account's owner the one who chose to
  spend the login;
- a new login is a different act with its own tool, `open_login_window`;
- pre-existing jars are not grandfathered.
"""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from .conftest import TEST_DOMAIN, service_with_ui
# The vault fixture (temp DPC_HOME + in-memory keyring) lives beside the
# audit tests; importing it is how pytest shares a fixture across modules.
from .test_web_audit import vault_home  # noqa: F401


def _read_audit(home: Path, agent_id: str) -> list[dict]:
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fresh_cookies(domain=TEST_DOMAIN):
    return [{"name": "s", "value": "v", "domain": f".{domain}", "path": "/",
             "expires": int(time.time()) + 3600, "secure": True, "httponly": True,
             "samesite": "Lax"}]


def _ctx(home: Path, ui: bool = False):
    """`ui=True` puts a person at the screen who approves.

    The headless gate refuses when there is nobody to ask, and "nobody to
    ask" now includes a context carrying no service at all — a browse that
    means to reach the browser has to say who is watching."""
    root = home / "agents" / "agent_a"
    root.mkdir(parents=True, exist_ok=True)
    if ui:
        return types.SimpleNamespace(
            agent_root=root, dpc_service=service_with_ui(),
        )
    return types.SimpleNamespace(agent_root=root)


def _browse(ctx, **kw):
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    return asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://{TEST_DOMAIN}/my", use_auth=TEST_DOMAIN, **kw
    ))


def _approve(domain=TEST_DOMAIN, agent="agent_a"):
    from dpc_client_core import web_auth

    web_auth.save_cookies(
        agent, domain, _fresh_cookies(domain),
        approved_via=web_auth.APPROVAL_VIA_LOGIN_WINDOW,
    )


@pytest.mark.parametrize("keep_open", [False, True])
def test_a_jar_with_no_approval_is_refused_headed_and_headless(
    vault_home, keep_open,
):
    """The case the old gate let through: cookies present, nobody ever
    approved them. The headed spelling is the one that used to be exempt."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _fresh_cookies())
    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"]

    answer = _browse(_ctx(vault_home), keep_open=keep_open)

    entries = _read_audit(vault_home, "agent_a")
    assert len(entries) == 1
    assert entries[0]["status"] == "auth_denied:no_approved_login"
    assert "No approved login" in answer
    assert "open_login_window" in answer


def test_the_refusal_happens_before_any_browser_is_launched(vault_home, monkeypatch):
    """The assertion that separates a real gate from a decorative one.

    A version that audits the denial and then browses anyway satisfies every
    count assertion. It does not satisfy this one.
    """
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    opened: list[str] = []
    for name in ("_auth_browse_html", "_get_or_create_session_async",
                 "_get_or_create_fetch_session", "_browse_sync"):
        monkeypatch.setattr(
            browser_mod, name,
            lambda *a, _n=name, **kw: opened.append(_n) or (_ for _ in ()).throw(
                AssertionError(f"{_n} was called after an approval denial")
            ),
        )

    _browse(_ctx(vault_home))
    _browse(_ctx(vault_home), keep_open=True)

    assert opened == [], f"a denied browse still reached: {opened}"


def test_the_refusal_names_no_deleted_ui(vault_home):
    """The web-auth UI was deleted in c2cfab07 and the firewall never held
    a domain list. A refusal that sends the agent to either is unactionable."""
    answer = _browse(_ctx(vault_home))

    assert "privacy_rules" not in answer
    assert "web-auth UI" not in answer


def test_an_approved_login_goes_through(vault_home, monkeypatch):
    """A gate that refuses everything is not a gate, it is an outage.

    The context here carries a UI on purpose. It used to carry none, and
    the headless gate read that as permission to skip itself — so this test
    passed by exercising the hole rather than the path."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    _approve()
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    _browse(_ctx(vault_home, ui=True))

    entries = _read_audit(vault_home, "agent_a")
    assert [e["status"] for e in entries] == [
        "headless_requested", "headless_approved", 200,
    ]


def test_an_approved_login_with_nobody_at_the_screen_is_still_refused(
    vault_home, monkeypatch,
):
    """The other half, and the hole the test above used to sit in: an
    approval authorises spending the login, not spending it unwatched. A
    context with no service is not a context with a silent yes."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    _approve()
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("the browser was reached with nobody to approve it")
        ),
    )

    answer = _browse(_ctx(vault_home))

    assert "no UI client is connected" in answer
    assert [e["status"] for e in _read_audit(vault_home, "agent_a")] == [
        "headless_no_ui",
    ]


def test_a_domain_with_no_approval_is_refused_and_the_message_names_the_approved(
    vault_home,
):
    """The refusal must say which logins ARE approved, or the agent is left
    guessing at a vault it cannot see."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    _approve("real-site.net")

    answer = asyncio.run(browser_mod.browse_page(
        _ctx(vault_home),
        url="https://other-site.example/my", use_auth="other-site.example",
    ))

    entries = _read_audit(vault_home, "agent_a")
    assert entries[-1]["status"] == "auth_denied:no_approved_login"
    assert "real-site.net" in answer, "the message must name the approved login"


def test_an_unapproved_jar_is_not_named_as_approved(vault_home):
    """Fail-closed migration, stated as a test: a jar carrying only the old
    `authenticated_at` stamp must not appear in the list of approvals."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", "legacy-site.net", _fresh_cookies("legacy-site.net"))
    _approve("real-site.net")

    answer = asyncio.run(_browse_other(vault_home))

    assert "real-site.net" in answer
    assert "legacy-site.net" not in answer


async def _browse_other(home: Path):
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    return await browser_mod.browse_page(
        _ctx(home), url="https://other-site.example/my",
        use_auth="other-site.example",
    )


def test_a_subdomain_spelling_now_reaches_the_jar_of_its_registrable_domain(vault_home):
    """`www.real-site.net` and `real-site.net` are one jar under the PSL
    resolver, so a login at either spelling authorises the other."""
    from dpc_client_core import web_auth

    _approve("real-site.net")

    assert web_auth.load_cookies("agent_a", "www.real-site.net") is not None
    assert web_auth.get_auth_status("agent_a", "login.real-site.net")["has_cookies"]
    assert web_auth.is_approved("agent_a", "login.real-site.net")
