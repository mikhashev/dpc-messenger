"""A login window that nobody answered for must change nothing on disk.

The window starts clean, so the site hands it the jar it hands any
anonymous visitor: x.com returns `guest_id`, `gt`, `__cf_bm` before anyone
has typed a character. Those cookies are in scope, and `save_cookies`
*replaces* a jar rather than merging into it. So while the window polled
straight into the vault, opening one to look at a site and walking away
replaced a working login with an anonymous one — silently, with nothing to
undo it, and on the branch that is the common case: the timeout fired twice
on the first day this was used.

The fix is an ordering. The window holds its snapshot in memory and writes
only where a person answered yes, cookies and approval in the same call. A
no, a silence and a departed UI are then true no-ops, which is what the
byte-for-byte comparisons below measure — a Fernet blob is re-randomised on
every write, so an unchanged file proves no write was attempted at all, not
merely that the contents came out equal.

The identity check in `save_cookies` stays as the second line, and the last
test here is its case: a yes after signing in as somebody else grants to
that account, not to the one the jar used to hold.
"""

import time

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401
from .test_an_approval_is_a_human_act_not_a_cookie import (  # noqa: F401
    _Api,
    _guest_cookies,
    _logged_in_cookies,
    _run_login_window,
)


AGENT = "agent_a"


def _vault_bytes(home) -> bytes:
    path = home / "agents" / AGENT / "web_credentials.enc"
    return path.read_bytes() if path.exists() else b""


def _seed_approved_login(domain=TEST_DOMAIN, value="real-session-token"):
    """A jar somebody already signed into and approved — what a login window
    opened afterwards is capable of destroying."""
    from dpc_client_core import web_auth

    cookies = [{
        "name": "auth_token", "value": value, "domain": f".{domain}",
        "path": "/", "expires": int(time.time()) + 86400,
        "secure": True, "httponly": True, "samesite": "Lax",
    }]
    web_auth.save_cookies(
        AGENT, domain, cookies,
        approved_via=web_auth.APPROVAL_VIA_LOGIN_WINDOW,
    )
    return cookies


@pytest.mark.parametrize(
    "answer, window_times_out, expected",
    [
        ("ignore", True, "stayed open"),
        ("ignore", False, "not answered"),
        ("reject", False, "declined"),
    ],
    ids=["window_timed_out", "question_unanswered", "person_said_no"],
)
def test_a_window_nobody_approved_leaves_the_jar_byte_for_byte(
    vault_home, monkeypatch, answer, window_times_out, expected,
):
    """Three ways to not say yes, one outcome on disk: nothing happened.

    The window collects the site's guest cookies either way — that is what a
    clean profile gets handed — and every one of them is in scope for the
    jar it would replace."""
    from dpc_client_core import web_auth

    _seed_approved_login()
    before = _vault_bytes(vault_home)
    granted = web_auth.get_approval(AGENT, TEST_DOMAIN)

    out = _run_login_window(
        monkeypatch, vault_home, api=_Api(answer),
        cookies=_guest_cookies(), window_times_out=window_times_out,
    )

    assert expected in out
    assert _vault_bytes(vault_home) == before, "the vault was not written at all"
    assert web_auth.get_approval(AGENT, TEST_DOMAIN) == granted


def test_the_guest_jar_never_replaces_a_real_session(vault_home, monkeypatch):
    """The measured loss, as a test. `save_cookies` replaces a jar, the
    guest cookies are in scope, and the poll ran every two seconds — so the
    session token was gone before the person had decided anything."""
    from dpc_client_core import web_auth

    _seed_approved_login()
    _run_login_window(
        monkeypatch, vault_home, api=_Api("ignore"), cookies=_guest_cookies(),
    )

    kept = web_auth.load_cookies(AGENT, TEST_DOMAIN)
    assert [c["name"] for c in kept] == ["auth_token"]
    assert kept[0]["value"] == "real-session-token"
    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True


def test_a_jar_emptied_as_the_window_dies_does_not_erase_the_sign_in(vault_home):
    """Polling into memory keeps what the close path cannot see, and the
    last poll of a dying window is the one likely to see nothing: the
    snapshot is replaced only by a later one that has something in it."""
    from dpc_client_core import web_auth
    from dpc_client_core.dpc_agent.tools.browser import AuthBrowser
    from .test_an_approval_is_a_human_act_not_a_cookie import _StubContext, _pw

    ab = AuthBrowser(
        agent_id=AGENT, domains=[TEST_DOMAIN], headed=True, login_window=True,
    )
    ctx = _StubContext(cookies_payload=_pw(_logged_in_cookies()))
    ab._context = ctx
    ab.capture_login_cookies()
    ctx.cookies_payload = []
    ab.capture_login_cookies()

    assert ab.commit_login_cookies() == len(_logged_in_cookies())
    assert "auth_token" in {
        c["name"] for c in web_auth.load_cookies(AGENT, TEST_DOMAIN)
    }


def test_a_yes_writes_the_cookies_and_the_approval_in_one_write(
    vault_home, monkeypatch,
):
    """One write, not a save followed by an approval. Two of them leave a
    window in which the jar holds the new cookies under no decision, and
    make the approval a thing that lands on bytes somebody else put there."""
    from dpc_client_core import web_auth

    writes = {"n": 0}
    real_save = web_auth._save_vault

    def _counting_save(agent_id, vault):
        writes["n"] += 1
        return real_save(agent_id, vault)

    monkeypatch.setattr(web_auth, "_save_vault", _counting_save)

    out = _run_login_window(
        monkeypatch, vault_home, api=_Api("approve"),
        cookies=_guest_cookies(), extra_cookies=_logged_in_cookies()[-1:],
    )

    status = web_auth.get_auth_status(AGENT, TEST_DOMAIN)
    assert "approved at" in out
    assert writes["n"] == 1, "the whole window is one write, and it is the yes"
    assert status["has_cookies"] is True
    assert status["approved"]["at"] == status["authenticated_at"]
    assert "auth_token" in {
        c["name"] for c in web_auth.load_cookies(AGENT, TEST_DOMAIN)
    }


def test_a_yes_after_signing_in_as_somebody_else_grants_to_that_account(
    vault_home, monkeypatch,
):
    """The second line, still earning its place. The ordering decides
    *whether* anything is written; the identity marks decide *whose* login
    the recorded yes covers."""
    from dpc_client_core import web_auth

    _seed_approved_login(value="the-old-account")
    first = web_auth._load_vault(AGENT)["domains"][TEST_DOMAIN]

    _run_login_window(
        monkeypatch, vault_home, api=_Api("approve"),
        cookies=_guest_cookies(),
        extra_cookies=[{
            "name": "auth_token", "value": "the-new-account",
            "domain": f".{TEST_DOMAIN}", "path": "/",
            "expires": int(time.time()) + 86400,
        }],
    )

    entry = web_auth._load_vault(AGENT)["domains"][TEST_DOMAIN]
    assert "the-new-account" in {c["value"] for c in entry["cookies"]}
    assert entry["approved_identity"] == web_auth.identity_marks(entry["cookies"])
    assert set(entry["approved_identity"]) & set(first["approved_identity"]) == set()
