"""Approval is something a person does, not something a page load produces.

The first live run of the login window falsified the design it was built on.
The window opened clean and scoped to x.com, a page loaded, eight cookies
appeared — `guest_id`, `gt`, `personalization_id`, `__cf_bm` and friends, the
jar x.com hands any anonymous visitor — and the vault recorded
`approved: {via: "login_window"}`. No `auth_token`, no `ct0`; the bundle the
page pulled was even named `entry-client-logged-out`. Nobody had logged in.

Two things were wrong and both are pinned here. The approval fired from
`_persist_session_cookies`, which runs after every navigate, so it landed on
the first navigation rather than at the end. And "cookies appeared" was an
inference in the first place — the same error one level up from the defect
this day was spent removing, where `save_cookies` stamped `authenticated_at`
on every close.

What replaces it: the person is asked, and only their yes is written. A
cookie diff against the anonymous first load travels with the question as
context and decides nothing — the tests below hold that line by approving a
window whose only new cookie is a guest cookie, and refusing one that carries
a real session token.
"""

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


# ── the doubles ─────────────────────────────────────────────────────────


class _StubContext:
    """Enough Playwright BrowserContext surface to drive _open()/close()."""

    def __init__(self, cookies_payload=None):
        self.added: list[list[dict]] = []
        self.routes: list[tuple] = []
        self.cookies_payload = list(cookies_payload or [])

    def add_cookies(self, cookies):
        self.added.append(list(cookies))

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def new_page(self):
        return object()

    def cookies(self):
        return list(self.cookies_payload)

    def close(self):
        pass


class _Route:
    def __init__(self, url, *, method="GET", resource_type="script",
                 nav=False, frame_url=None):
        frame = types.SimpleNamespace(url=frame_url) if frame_url else None
        self.request = types.SimpleNamespace(
            url=url, method=method, resource_type=resource_type,
            is_navigation_request=lambda: nav, frame=frame,
        )
        self.continued = False
        self.aborted = False

    def continue_(self):
        self.continued = True

    def abort(self):
        self.aborted = True


class _Api:
    """The UI, answering the way the dialog does — or not answering."""

    def __init__(self, answer, has_clients=True):
        self.answer = answer
        self.has_clients = has_clients
        self.events: list[tuple[str, dict]] = []

    async def broadcast_event(self, name, payload):
        from dpc_client_core.dpc_agent.tools import browser as mod

        self.events.append((name, payload))
        if self.answer == "ignore":
            return
        entry = mod.get_pending_auth_approvals()[payload["request_id"]]
        entry["approved"] = self.answer == "approve"
        entry["event"].set()


def _guest_cookies(domain=TEST_DOMAIN):
    """What a site hands an anonymous visitor on first contact."""
    return [
        {"name": n, "value": "v", "domain": f".{domain}", "path": "/",
         "expires": int(time.time()) + 3600}
        for n in ("guest_id", "gt", "personalization_id")
    ]


def _logged_in_cookies(domain=TEST_DOMAIN):
    return _guest_cookies(domain) + [
        {"name": "auth_token", "value": "secret", "domain": f".{domain}",
         "path": "/", "expires": int(time.time()) + 86400},
    ]


def _pw(cookies):
    """Playwright shape — httpOnly/sameSite rather than the vault's snake."""
    return [dict(c, httpOnly=True, sameSite="Lax", secure=True) for c in cookies]


def _ctx(home: Path, local_api=None, agent_id="agent_a"):
    root = home / "agents" / agent_id
    root.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        agent_root=root,
        dpc_service=types.SimpleNamespace(local_api=local_api),
    )


def _audit(home: Path, agent_id="agent_a") -> list[dict]:
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line
            in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _statuses(home, agent_id="agent_a"):
    return [r.get("status") or r.get("action") for r in _audit(home, agent_id)]


def _run_login_window(
    monkeypatch, home, *, api, cookies, extra_cookies=None,
    close_after_polls=None, window_times_out=False, ui_leaves=False,
):
    """Drive `open_login_window` end to end against stubs.

    `cookies` is what the window holds at the first poll — the anonymous
    baseline. `extra_cookies` appear afterwards, the way a login does.
    """
    from dpc_client_core.dpc_agent.tools import browser as mod

    ctx = _StubContext(cookies_payload=_pw(cookies))

    class _StubBrowser:
        def new_context(self, **kwargs):
            return ctx

        def on(self, *_a, **_kw):
            pass

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(), raising=False,
    )
    monkeypatch.setattr(mod.AuthBrowser, "navigate", lambda self, url: "")
    monkeypatch.setattr(mod, "_LOGIN_WINDOW_POLL_SEC", 0.0)
    monkeypatch.setattr(mod, "_LOGIN_APPROVAL_TIMEOUT_SEC", 0.05)

    polls = {"n": 0}
    # The loop captures, then asks whether the window is gone. Cookies that
    # arrive after the first capture are what a sign-in looks like, so they
    # need a second poll to be seen at all.
    last_poll = close_after_polls or (2 if extra_cookies else 1)

    def _gone(self):
        polls["n"] += 1
        if polls["n"] == 1 and extra_cookies:
            ctx.cookies_payload = _pw(cookies + extra_cookies)
        done = (not window_times_out) and polls["n"] >= last_poll
        if done and ui_leaves:
            api.has_clients = False
        return done

    monkeypatch.setattr(mod.AuthBrowser, "window_is_gone", _gone)

    if window_times_out:
        clock = {"t": 0.0}

        def _monotonic():
            clock["t"] += 20.0
            return clock["t"]

        monkeypatch.setattr(
            mod, "time", types.SimpleNamespace(
                monotonic=_monotonic, sleep=time.sleep, time=time.time,
            ),
        )

    async def _run(session, method_name, *a, **kw):
        kw.pop("_touch", None)
        kw.pop("_timeout", None)
        return getattr(session, method_name)(*a, **kw)

    monkeypatch.setattr(mod, "_run_in_session", _run)

    tool_ctx = _ctx(home, local_api=api)
    return asyncio.run(mod.open_login_window(tool_ctx, domain=TEST_DOMAIN))


# ── the decision ────────────────────────────────────────────────────────


def test_guest_cookies_alone_never_produce_an_approval(vault_home, monkeypatch):
    """The measured failure, as a test. A clean window, a page load, the
    anonymous jar — and no human. Without an answer there is no approval,
    however convincing the jar looks."""
    from dpc_client_core import web_auth

    api = _Api("ignore")
    out = _run_login_window(
        monkeypatch, vault_home, api=api, cookies=_guest_cookies(),
    )

    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"] is False
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert "No approved login" in out


def test_an_explicit_yes_is_what_writes_the_approval(vault_home, monkeypatch):
    from dpc_client_core import web_auth

    api = _Api("approve")
    out = _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
    )

    approval = web_auth.get_approval("agent_a", TEST_DOMAIN)
    assert approval is not None
    assert approval["via"] == web_auth.APPROVAL_VIA_LOGIN_WINDOW
    assert approval["at"].endswith("Z")
    assert "approved at" in out


def test_a_no_is_a_no(vault_home, monkeypatch):
    from dpc_client_core import web_auth

    api = _Api("reject")
    out = _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
    )

    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert "declined" in out


def test_an_unanswered_question_is_not_a_yes(vault_home, monkeypatch):
    """Silence expiring into consent is the whole shape being removed. A real
    session cookie in the jar does not change the answer, because the jar was
    never the thing being asked."""
    from dpc_client_core import web_auth

    api = _Api("ignore")
    out = _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
    )

    assert api.events, "the question was put"
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert "not answered" in out


def test_a_yes_is_honoured_when_only_guest_cookies_appeared(vault_home, monkeypatch):
    """The diff is context, never the decision. A person who says yes over a
    jar that looks anonymous is still the authority — the alternative is a
    heuristic wearing a dialog."""
    from dpc_client_core import web_auth

    api = _Api("approve")
    _run_login_window(monkeypatch, vault_home, api=api, cookies=_guest_cookies())

    assert web_auth.is_approved("agent_a", TEST_DOMAIN) is True


def test_a_window_is_not_opened_with_nobody_at_the_screen(vault_home, monkeypatch):
    """A headed browser nobody is looking at cannot collect a password, so
    the refusal comes before the window rather than after it."""
    from dpc_client_core import web_auth

    api = _Api("approve", has_clients=False)
    out = _run_login_window(
        monkeypatch, vault_home, api=api, cookies=_logged_in_cookies(),
    )

    assert api.events == [], "no point broadcasting to nobody"
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert web_auth.list_domains("agent_a") == [], "no window, no cookies"
    assert "needs a person at the screen" in out


def test_a_ui_that_leaves_mid_window_cannot_be_asked(vault_home, monkeypatch):
    """The ask-time half of the same fact. `broadcast_event` drops a request
    nobody is connected for, so waiting on it could only ever expire and be
    read as a refusal no person made — say what happened instead."""
    from dpc_client_core import web_auth

    api = _Api("approve")
    out = _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
        ui_leaves=True,
    )

    assert api.events == [], "the UI was gone by the time the question arose"
    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"] is False
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert "no UI client" in out
    assert "login_window_no_ui" in _statuses(vault_home)


def test_no_local_api_at_all_means_the_window_never_opens(vault_home, monkeypatch):
    """The same refusal as a UI that says it has no clients, reached the
    other way. `local_api is None` used to skip the check instead of
    tripping it, so the one case where nobody could possibly be watching
    was the one case that got a headed window on the screen."""
    from dpc_client_core import web_auth

    out = _run_login_window(
        monkeypatch, vault_home, api=None, cookies=_logged_in_cookies(),
    )

    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert web_auth.list_domains("agent_a") == [], "no window, no cookies"
    assert "needs a person at the screen" in out
    assert "no UI client" in out


def test_a_window_that_times_out_still_asks_and_still_needs_a_yes(
    vault_home, monkeypatch,
):
    """A sign-in can outlast the window: the live run took ten minutes. The
    window closing on the clock is not a refusal — but it is not consent
    either, so the question is still put and still has to be answered."""
    from dpc_client_core import web_auth

    api = _Api("ignore")
    out = _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_logged_in_cookies(), window_times_out=True,
    )

    assert api.events, "the person is still asked after a window timeout"
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert "stayed open" in out


def test_a_window_with_no_cookies_at_all_asks_nobody(vault_home, monkeypatch):
    """There is nothing to approve, so the person is not interrupted."""
    api = _Api("approve")
    out = _run_login_window(monkeypatch, vault_home, api=api, cookies=[])

    assert api.events == []
    assert "No login was captured" in out


def test_an_older_jar_does_not_make_an_empty_window_look_successful(
    vault_home, monkeypatch,
):
    """The question is what this window collected, not what the vault holds.
    Reading the jar would let a window where nothing happened inherit the
    credit for a previous one — and put a question to the person about a
    sign-in that never took place."""
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _logged_in_cookies())
    api = _Api("approve")
    out = _run_login_window(monkeypatch, vault_home, api=api, cookies=[])

    assert api.events == []
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert "No login was captured" in out


# ── the question and the answer are both in the trail ───────────────────


def test_the_request_and_the_answer_are_audited_separately(vault_home, monkeypatch):
    """One row saying a person was asked, one saying what they said. Folding
    them into a single `login_approved` leaves no way to see the times a
    question went out and came back empty."""
    api = _Api("approve")
    _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
    )

    rows = _audit(vault_home)
    asked = [r for r in rows if r.get("action") == "login_approval_requested"]
    answered = [r for r in rows if r.get("action") == "login_approval_answer"]
    granted = [r for r in rows if r.get("action") == "login_approved"]

    assert len(asked) == 1 and len(answered) == 1 and len(granted) == 1
    assert answered[0]["result"] == "approved"
    assert rows.index(asked[0]) < rows.index(answered[0]) < rows.index(granted[0])


def test_a_refusal_is_audited_as_a_refusal_and_a_silence_as_a_silence(
    vault_home, monkeypatch,
):
    """Three outcomes that mean three different things to whoever reads the
    log: declined, unanswered, and nobody there to ask."""
    _run_login_window(
        monkeypatch, vault_home, api=_Api("reject"), cookies=_guest_cookies(),
    )
    assert "login_window_rejected" in _statuses(vault_home)

    _run_login_window(
        monkeypatch, vault_home, api=_Api("ignore"),
        cookies=_guest_cookies(), close_after_polls=1,
    )
    assert "login_window_timeout" in _statuses(vault_home)

    # No service at all is the third: the window is not opened, because
    # nobody could be asked about it or look at it.
    _run_login_window(
        monkeypatch, vault_home, api=None, cookies=_guest_cookies(),
    )
    assert "login_window_denied:headless_no_ui" in _statuses(vault_home)


def test_the_question_names_the_agent_the_chat_and_the_diff(vault_home, monkeypatch):
    """A person approving a login has to know who is asking. The diff rides
    along as context — it is in the payload, and in nothing that grants."""
    api = _Api("approve")
    _run_login_window(
        monkeypatch, vault_home, api=api,
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
    )

    name, payload = api.events[0]
    assert name == "web_auth_headless_approval_request"
    assert payload["kind"] == "login_window"
    assert payload["domain"] == TEST_DOMAIN
    assert {"agent_name", "conversation_id", "conversation_title"} <= set(payload)
    assert payload["evidence"]["new_cookie_names"] == ["auth_token"]
    assert payload["evidence"]["baseline_count"] == 3


# ── not on navigate ─────────────────────────────────────────────────────


def _navigable(monkeypatch, cookies, **kwargs):
    from dpc_client_core.dpc_agent.tools import browser as mod

    ctx = _StubContext(cookies_payload=_pw(cookies))

    class _StubBrowser:
        def new_context(self, **kw):
            return ctx

        def on(self, *_a, **_kw):
            pass

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(), raising=False,
    )
    ab = mod.AuthBrowser(agent_id="agent_a", **kwargs)
    ab._open()
    mod._active_camoufox_browsers.discard(ab)
    ab._page = types.SimpleNamespace(
        url=f"https://{TEST_DOMAIN}/",
        goto=lambda url, **kw: types.SimpleNamespace(status=200),
    )
    monkeypatch.setattr(ab, "_wait_for_content_stable", lambda *a, **kw: None)
    monkeypatch.setattr(ab, "a11y_snapshot", lambda: ("", {}))
    return ab


def test_navigating_a_login_window_writes_nothing(vault_home, monkeypatch):
    """The mechanical half of the defect. `_persist_session_cookies` runs
    after every navigate, so an approval written there landed on the first
    page load — ten minutes before the person had finished, and regardless of
    whether they ever did. The cookies followed the same route, and for a
    login window they now go no further than memory."""
    from dpc_client_core import web_auth

    ab = _navigable(
        monkeypatch, _logged_in_cookies(),
        domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ab.navigate(f"https://{TEST_DOMAIN}/")

    assert web_auth.get_auth_status("agent_a", TEST_DOMAIN)["has_cookies"] is False
    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None
    assert ab._login_pending, "captured, not written"


def test_navigating_an_ordinary_session_records_no_approval(vault_home, monkeypatch):
    from dpc_client_core import web_auth

    ab = _navigable(monkeypatch, _logged_in_cookies(), domains=[TEST_DOMAIN])
    ab.navigate(f"https://{TEST_DOMAIN}/")

    assert web_auth.get_approval("agent_a", TEST_DOMAIN) is None


# ── one identity, one door ──────────────────────────────────────────────


def test_a_use_auth_session_does_not_load_browser_state(vault_home, monkeypatch):
    """The second source of identity. The live run rendered x.com/home logged
    in as an account nobody had approved while the vault jar held only guest
    cookies: browser_state.json carried 230 cookies across 55 domains,
    `.x.com` among them, and `_open` loaded it before overlaying the vault."""
    state = vault_home / "agents" / "agent_a" / "browser_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps({"cookies": [{"name": "auth_token", "value": "someone_else",
                                 "domain": f".{TEST_DOMAIN}", "path": "/"}],
                    "origins": []}),
        encoding="utf-8",
    )

    seen: list[dict] = []
    from dpc_client_core.dpc_agent.tools import browser as mod

    ctx = _StubContext()

    class _StubBrowser:
        def new_context(self, **kwargs):
            seen.append(dict(kwargs))
            return ctx

        def on(self, *_a, **_kw):
            pass

    class _StubCm:
        def __enter__(self):
            return _StubBrowser()

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(
        "camoufox.sync_api.Camoufox", lambda **kw: _StubCm(), raising=False,
    )
    ab = mod.AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    ab._open()
    mod._active_camoufox_browsers.discard(ab)

    assert "storage_state" not in seen[0]
    assert ctx.added == [[]] or ctx.added == [], (
        "the only identity is the vault jar for the scope, and it is empty here"
    )


# ── a login window can actually be logged into ──────────────────────────


@pytest.mark.parametrize(
    "url, method, kind",
    [
        ("https://accounts.google.com/gsi/client", "GET", "script"),
        ("https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid.auth.js",
         "GET", "script"),
        ("https://edge.prelude.dev/v1/signals", "POST", "fetch"),
    ],
)
def test_a_login_window_reaches_the_hosts_a_sign_in_needs(
    vault_home, url, method, kind,
):
    """Measured from the live run: these three were refused inside the window
    built for logging in — the two identity providers as unlisted scripts, the
    anti-bot gate on POST, which no manifest can admit. The human's screen
    read "Something went wrong"."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    route = _Route(url, method=method, resource_type=kind,
                   frame_url=f"https://{TEST_DOMAIN}/i/flow/login")
    ab._domain_route_gate(route)

    assert route.continued is True
    assert route.aborted is False


def test_the_widening_belongs_to_login_windows_only(vault_home):
    """An ordinary authenticated session is the opposite case: it carries a
    stored login, so a foreign host is exactly what must not be reachable."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    route = _Route("https://accounts.google.com/gsi/client",
                   frame_url=f"https://{TEST_DOMAIN}/home")
    ab._domain_route_gate(route)

    assert route.aborted is True


def test_an_ungated_login_window_still_keeps_only_its_own_site(vault_home):
    """The read side widened; the write side did not. A window that walks to
    an identity provider carries nothing of it into the snapshot, so a yes
    cannot commit one."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    scoped = ab._scope_cookies_by_etld1(_pw([
        {"name": "SID", "value": "g", "domain": ".google.com", "path": "/"},
        {"name": "auth_token", "value": "t", "domain": f".{TEST_DOMAIN}",
         "path": "/"},
    ]))
    ab._login_pending = scoped
    written = ab.commit_login_cookies()

    assert list(scoped) == [TEST_DOMAIN] and written == 1
    assert [r["domain"] for r in web_auth.list_domains("agent_a")] == [TEST_DOMAIN]
    assert web_auth.load_cookies("agent_a", "google.com") is None


def test_a_login_window_cannot_reach_the_ordinary_cookie_writeback(
    vault_home, caplog,
):
    """One writer, and it is the one a yes goes through. `_sync_cookies_to
    _vault` is the path every other session uses on navigate and on close;
    reached from a login window it refuses and says so, so a future caller
    wiring the two together cannot quietly restore the ordering defect."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    with caplog.at_level("ERROR"):
        written = ab._sync_cookies_to_vault(_pw(_logged_in_cookies()))

    assert written == 0
    assert web_auth.list_domains("agent_a") == []
    assert any("commit_login_cookies" in r.getMessage() for r in caplog.records)


def test_the_pre_navigation_check_agrees_with_the_widened_gate(vault_home):
    """Two layers disagreeing is how a hole hides — and here it would be the
    opposite hole: a gate that admits an identity provider while the check in
    front of it refuses to navigate there."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ab._check_domain("https://accounts.google.com/signin")  # must not raise


def test_where_an_ungated_window_went_is_still_recorded(vault_home):
    """Enforcing nothing is not the same as seeing nothing."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(
        agent_id="agent_a", domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ab._domain_route_gate(_Route(
        "https://accounts.google.com/gsi/client",
        frame_url=f"https://{TEST_DOMAIN}/i/flow/login",
    ))

    row = next(r for r in _audit(vault_home)
               if r.get("action") == "login_window_passthrough")
    assert row["dest_host"] == "accounts.google.com"
    assert row["result"] == "ok"


# ── the learning mechanism sees every refusal ───────────────────────────


def test_a_host_refused_on_the_outright_path_is_recorded(vault_home):
    """The blind spot, measured: `accounts.google.com` was refused on the
    manifest path and recorded, `edge.prelude.dev` was refused outright and
    was not recorded at all — so the promotion UI could never learn it
    existed, and a site broken that way stayed broken with no trace."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    ab._domain_route_gate(_Route(
        "https://edge.prelude.dev/v1/signals", method="POST",
        resource_type="fetch", frame_url=f"https://{TEST_DOMAIN}/home",
    ))

    entry = web_auth.load_cdn_refusals("agent_a")[TEST_DOMAIN]["edge.prelude.dev"]
    assert entry["count"] == 1
    assert entry["first_seen"]
    assert entry["reasons"] == {web_auth.REFUSAL_REASON_BLOCKED: 1}


def test_the_refusal_says_which_branch_made_it(vault_home):
    """Promoting a host into the manifest fixes an `unlisted` refusal and does
    nothing for a `blocked` one, which is decided on method before the
    manifest is read. A UI that cannot tell them apart offers a fix that does
    not fix."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    ab._domain_route_gate(_Route(
        "https://static.example-cdn.net/main.js",
        frame_url=f"https://{TEST_DOMAIN}/home",
    ))
    ab._domain_route_gate(_Route(
        "https://edge.prelude.dev/v1/signals", method="POST",
        resource_type="fetch", frame_url=f"https://{TEST_DOMAIN}/home",
    ))

    refused = web_auth.load_cdn_refusals("agent_a")[TEST_DOMAIN]
    assert refused["static.example-cdn.net"]["reasons"] == {
        web_auth.REFUSAL_REASON_UNLISTED: 1
    }
    assert refused["edge.prelude.dev"]["reasons"] == {
        web_auth.REFUSAL_REASON_BLOCKED: 1
    }


def test_repeat_outright_refusals_are_counted_at_close(vault_home):
    """The coalescing that keeps one page load from writing hundreds of rows
    must not lose the count the refusal exists to carry."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    for _ in range(6):
        ab._domain_route_gate(_Route(
            "https://edge.prelude.dev/v1/signals", method="POST",
            resource_type="fetch", frame_url=f"https://{TEST_DOMAIN}/home",
        ))
    ab._flush_gate_audit()

    entry = web_auth.load_cdn_refusals("agent_a")[TEST_DOMAIN]["edge.prelude.dev"]
    assert entry["count"] == 6


def test_an_outright_refusal_still_authorises_nothing(vault_home):
    """The property the two-file split exists for, extended to the branch that
    now writes into it: the gate reads the manifest and never the
    refusals."""
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser

    ab = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    ab._domain_route_gate(_Route(
        "https://edge.prelude.dev/v1/signals", method="POST",
        resource_type="fetch", frame_url=f"https://{TEST_DOMAIN}/home",
    ))

    again = AuthBrowser(agent_id="agent_a", domains=[TEST_DOMAIN])
    second = _Route(
        "https://edge.prelude.dev/v1/signals", method="POST",
        resource_type="fetch", frame_url=f"https://{TEST_DOMAIN}/home",
    )
    again._domain_route_gate(second)

    assert second.aborted is True
    assert second.continued is False
