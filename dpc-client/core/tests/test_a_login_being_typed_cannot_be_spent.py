"""While a login window for a site is open, that site's jar is not spendable.

`_login_windows` was written and never read: `get_login_windows()` had no
production caller, so "a login for S is open right now" was state nobody
asked about. The race it exists to close is real, because the two
registries are separate — an open login window lives in `_login_windows`
while `browse_page` resolves `_active_browser_sessions` or builds a fresh
headless browser, so neither path could see the other.

What that costs, concretely: mid-login the jar for S holds the *previous*
approval beside the cookies the window has already polled into it. A
`browse_page(use_auth=S)` in that gap spends a half-written session under a
yes that was given for a different one.
"""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from .conftest import TEST_DOMAIN, service_with_ui
from .test_web_audit import vault_home  # noqa: F401


AGENT = "agent_a"


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """Both browse paths stubbed for every test in this file.

    Not tidiness: the tests here assert that a browse is *refused*, and a
    refusal that stops working reaches a real Camoufox against a real site.
    That happened once while falsifying these very tests — the mutation
    that removed the guard launched a browser and loaded wikipedia.org.
    The stubs make the failure an assertion instead of a page load.
    """
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    monkeypatch.setattr(
        browser_mod, "_get_or_create_session_async",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("a headed session was opened for real")
        ),
    )
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>stub</h1></body></html>",
    )


@pytest.fixture
def open_login_window_for():
    """Put a stand-in login window into the registry and take it out again.

    The registry is module state; a test that leaves an entry in it hands
    the next test a site it can never browse."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    placed: list[str] = []

    def _place(etld1s, agent_id=AGENT):
        browser_mod.get_login_windows()[agent_id] = types.SimpleNamespace(
            _etld1s=set(etld1s),
        )
        placed.append(agent_id)

    yield _place

    for agent_id in placed:
        browser_mod.get_login_windows().pop(agent_id, None)


def _cookies(domain=TEST_DOMAIN):
    return [{"name": "auth_token", "value": "t" * 40, "domain": f".{domain}",
             "path": "/", "expires": int(time.time()) + 3600, "secure": True,
             "httponly": True, "samesite": "Lax"}]


def _approved_ctx(home: Path, domain=TEST_DOMAIN):
    from dpc_client_core import web_auth

    web_auth.save_cookies(
        AGENT, domain, _cookies(domain),
        approved_via=web_auth.APPROVAL_VIA_LOGIN_WINDOW,
    )
    root = home / "agents" / AGENT
    root.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(agent_root=root, dpc_service=service_with_ui())


def _statuses(home: Path, agent_id=AGENT):
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line).get("status")
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _browse(ctx, domain=TEST_DOMAIN, **kw):
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    return asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://{domain}/home", use_auth=domain, **kw
    ))


@pytest.mark.parametrize("keep_open", [False, True])
def test_a_browse_is_refused_while_that_sites_login_window_is_open(
    vault_home, open_login_window_for, keep_open,
):
    """Headed too: `keep_open=True` spends the stored login exactly as
    headless does, and the half-written session is the same one."""
    ctx = _approved_ctx(vault_home)
    open_login_window_for([TEST_DOMAIN])

    answer = _browse(ctx, keep_open=keep_open)

    assert "login window" in answer
    assert TEST_DOMAIN in answer
    assert _statuses(vault_home) == ["auth_denied:login_in_progress"]


def test_the_refusal_comes_before_any_browser(vault_home, open_login_window_for,
                                               monkeypatch):
    """A version that audits and browses anyway satisfies every message
    assertion above and closes nothing."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ctx = _approved_ctx(vault_home)
    open_login_window_for([TEST_DOMAIN])
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("the headless browser ran while a login was typed")
        ),
    )

    _browse(ctx)
    _browse(ctx, keep_open=True)


def test_a_subdomain_spelling_is_refused_by_the_same_window(
    vault_home, open_login_window_for,
):
    """The window is scoped to the registrable domain and so is the jar, so
    a subdomain spelling must not walk around the refusal."""
    ctx = _approved_ctx(vault_home)
    open_login_window_for([TEST_DOMAIN])

    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    answer = asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://www.{TEST_DOMAIN}/home", use_auth=f"www.{TEST_DOMAIN}",
    ))

    assert "login window" in answer


def test_another_site_is_untouched_by_an_open_login_window(
    vault_home, open_login_window_for, monkeypatch,
):
    """A gate that stops everything while any login is in flight would make
    one sign-in block the agent's whole working set."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ctx = _approved_ctx(vault_home, "real-site.net")
    open_login_window_for([TEST_DOMAIN])
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    _browse(ctx, "real-site.net")

    assert "auth_denied:login_in_progress" not in _statuses(vault_home)


def test_the_registry_empties_and_the_browse_works_again(
    vault_home, open_login_window_for, monkeypatch,
):
    """The refusal is for the duration of the window, not for the site.
    `open_login_window` pops the entry in its `finally`, so nothing here
    should survive it."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ctx = _approved_ctx(vault_home)
    open_login_window_for([TEST_DOMAIN])
    assert "login window" in _browse(ctx)

    browser_mod.get_login_windows().pop(AGENT)
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    assert "login window" not in _browse(ctx)


def test_another_agents_login_window_does_not_block_this_one(
    vault_home, open_login_window_for, monkeypatch,
):
    """The vault is per agent and so is the window. agent_b typing a
    password is not a reason to stop agent_a."""
    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    ctx = _approved_ctx(vault_home)
    open_login_window_for([TEST_DOMAIN], agent_id="agent_b")
    monkeypatch.setattr(
        browser_mod, "_auth_browse_html",
        lambda *a, **kw: "<html><body><h1>ok</h1></body></html>",
    )

    assert "login window" not in _browse(ctx)
