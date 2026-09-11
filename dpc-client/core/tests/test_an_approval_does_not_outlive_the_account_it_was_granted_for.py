"""An approval belongs to one login, and dies with it.

The carry-forward in `save_cookies` was written for the writeback: a
navigate or a close refreshes the cookie bytes, and refreshed bytes are
neither a re-approval nor a revocation. Across a change of account it was
wrong. The jar for a site is approved while signed in as account A; the
person opens a login window — clean profile, so the site shows its sign-in
page — and signs in as B; the window's polling writes B's cookies through
`save_cookies` with no `approved_via`, and A's yes lands on B's session
before anybody has been asked anything.

What replaces it is a set of hashes over the identity-bearing cookies,
stored beside the approval and compared by intersection. The two halves
pinned here are the two directions it can fail in: a different account must
lose the approval, and a site rotating its own session cookie must keep it.
"""

import time
import types

import pytest

from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


AGENT = "agent_a"

# Long enough to be a secret rather than a setting, which is what
# `IDENTITY_MIN_VALUE_LEN` asks of a value before it counts as identity.
_A_TOKEN = "a" * 40
_B_TOKEN = "b" * 40


def _cookie(name, value, *, httponly, ttl=86400):
    return {
        "name": name, "value": value, "domain": f".{TEST_DOMAIN}", "path": "/",
        "expires": int(time.time()) + ttl, "secure": True,
        "httponly": httponly, "samesite": "Lax",
    }


def _account(token, *, rotation="0"):
    """A jar shaped like a real signed-in one.

    Three cookies doing three different things: a long-lived httpOnly
    session token, a JS-readable CSRF token the site re-issues as you
    browse, and an httpOnly edge cookie the CDN rotates on its own clock.
    The last one is what makes the rotation case a real test — it is
    identity-bearing by the classifier and it changes constantly, which is
    exactly where an intersection and an equality give different answers.

    The two short-lived values are derived from the token as well as from
    `rotation`, because a different account is served a different edge
    cookie too; holding them constant across accounts would quietly test
    the shared-value blind spot instead of the switch."""
    tag = token[0]
    return [
        _cookie("auth_token", token, httponly=True),
        _cookie("ct0", f"csrf-{tag}{rotation}" + "c" * 40, httponly=False),
        _cookie("__cf_bm", f"edge-{tag}{rotation}" + "e" * 40,
                httponly=True, ttl=1800),
    ]


def _approve(cookies):
    from dpc_client_core import web_auth

    web_auth.save_cookies(AGENT, TEST_DOMAIN, cookies)
    assert web_auth.record_approval(AGENT, TEST_DOMAIN) is not None


def _writeback(cookies):
    """What every non-approving writer does: cookie bytes, no decision."""
    from dpc_client_core import web_auth

    web_auth.save_cookies(AGENT, TEST_DOMAIN, cookies)


# ── the hole ────────────────────────────────────────────────────────────


def test_a_login_as_a_different_account_does_not_inherit_the_approval(vault_home):
    from dpc_client_core import web_auth

    _approve(_account(_A_TOKEN))
    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True

    _writeback(_account(_B_TOKEN))

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is False
    assert web_auth.get_auth_status(AGENT, TEST_DOMAIN)["has_cookies"], (
        "the cookies are still data and still land — only the yes is gone"
    )


def test_the_second_account_cannot_be_spent_through_browse_page(vault_home):
    """The consequence, at the surface that matters. The identity check
    lives in the vault; what it is for is the gate in front of the browser."""
    import asyncio

    from dpc_client_core.dpc_agent.tools import browser as browser_mod

    from .conftest import service_with_ui

    _approve(_account(_A_TOKEN))
    _writeback(_account(_B_TOKEN))

    root = vault_home / "agents" / AGENT
    root.mkdir(parents=True, exist_ok=True)
    ctx = types.SimpleNamespace(
        agent_root=root, dpc_service=service_with_ui(),
    )
    answer = asyncio.run(browser_mod.browse_page(
        ctx, url=f"https://{TEST_DOMAIN}/home", use_auth=TEST_DOMAIN,
    ))

    assert "No approved login" in answer


def test_the_drop_leaves_a_row_in_the_audit(vault_home):
    """The next thing that happens is a person being asked to approve a
    site they already approved. A silent drop leaves them guessing."""
    import json

    _approve(_account(_A_TOKEN))
    _writeback(_account(_B_TOKEN))

    rows = [
        json.loads(line) for line in
        (vault_home / "agents" / AGENT / "web_audit.jsonl")
        .read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    dropped = [r for r in rows if r.get("action") == "approval_dropped"]
    assert len(dropped) == 1
    assert dropped[0]["domain"] == TEST_DOMAIN
    assert dropped[0]["reason"] == "identity_changed"


def test_a_refresh_of_the_same_login_writes_no_such_row(vault_home):
    """The counterpart. A row per writeback would bury the one that means
    something in the ones that do not."""
    _approve(_account(_A_TOKEN))
    _writeback(_account(_A_TOKEN, rotation="1"))

    audit = vault_home / "agents" / AGENT / "web_audit.jsonl"
    assert not audit.exists() or "approval_dropped" not in audit.read_text(
        encoding="utf-8"
    )


def test_a_re_approval_after_the_switch_is_granted_for_the_new_account(vault_home):
    """The drop is not a lock-out: the person answers yes once more and the
    new session is approved — for itself, with its own marks."""
    from dpc_client_core import web_auth

    _approve(_account(_A_TOKEN))
    _writeback(_account(_B_TOKEN))
    assert web_auth.record_approval(AGENT, TEST_DOMAIN) is not None
    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True

    _writeback(_account("c" * 40))

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is False


# ── and the direction it must NOT fire in ───────────────────────────────


def test_a_rotated_session_cookie_does_not_drop_the_approval(vault_home):
    """Measured on this machine 2026-09-11: the site rotates its own CSRF
    token as you browse. A signal that read that as a new account would ask
    the person to re-approve on every page, which is how a gate gets turned
    off."""
    from dpc_client_core import web_auth

    _approve(_account(_A_TOKEN))

    _writeback(_account(_A_TOKEN, rotation="1"))
    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True

    _writeback(_account(_A_TOKEN, rotation="2"))
    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True


def test_a_shrinking_jar_that_still_holds_the_session_keeps_the_approval(vault_home):
    """A poll can catch the jar mid-write. Fewer cookies than before is not
    a different account as long as the session itself is one of them."""
    from dpc_client_core import web_auth

    _approve(_account(_A_TOKEN))
    _writeback([_cookie("auth_token", _A_TOKEN, httponly=True)])

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True


def test_an_expired_cookie_sweep_does_not_revoke_anything(vault_home):
    """`filter_expired` drops the short-lived cookies and writes the rest
    back. That is housekeeping, not a sign-in."""
    from dpc_client_core import web_auth

    cookies = _account(_A_TOKEN)
    _approve(cookies)
    _writeback(web_auth.filter_expired(cookies))

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True


# ── what the signal can and cannot see, stated rather than assumed ──────


def test_a_jar_with_no_identity_bearing_cookie_keeps_its_approval(vault_home):
    """The named blind spot. A site whose login rides entirely on cookies
    its own JavaScript can read gives this nothing to compare, and the
    approval is carried exactly as it was before the check existed.

    Asserting the limitation rather than leaving it to be discovered: the
    alternative — treating no-signal as a change — drops the approval on
    every writeback and demands re-approval forever."""
    from dpc_client_core import web_auth

    _approve([_cookie("session", _A_TOKEN, httponly=False)])
    _writeback([_cookie("session", _B_TOKEN, httponly=False)])

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True


def test_a_short_httponly_value_is_not_an_identity_anchor(vault_home):
    """`lang=en` is httpOnly on some sites and the same for every account.
    If a value that short counted, one setting shared between two accounts
    would carry the approval across the switch."""
    from dpc_client_core import web_auth

    _approve([_cookie("lang", "en", httponly=True),
              _cookie("auth_token", _A_TOKEN, httponly=True)])
    _writeback([_cookie("lang", "en", httponly=True),
                _cookie("auth_token", _B_TOKEN, httponly=True)])

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is False


def test_two_accounts_sharing_an_httponly_value_read_as_one(vault_home):
    """The third named limit, asserted so it is a known shape rather than a
    surprise. A site that hands two accounts the same httpOnly cookie leaves
    a mark that survives the switch, and nothing visible in a cookie jar can
    tell that mark apart from the session it sits beside."""
    from dpc_client_core import web_auth

    shared = _cookie("edge_pop", "p" * 40, httponly=True)
    _approve([_cookie("auth_token", _A_TOKEN, httponly=True), shared])
    _writeback([_cookie("auth_token", _B_TOKEN, httponly=True), shared])

    assert web_auth.is_approved(AGENT, TEST_DOMAIN) is True


def test_the_recorded_identity_is_hashes_and_not_the_tokens(vault_home):
    """The marks are copied forward on every writeback. A session token is
    the one thing in this file worth stealing, so it is not what is stored."""
    from dpc_client_core import web_auth

    _approve(_account(_A_TOKEN))
    entry = web_auth._load_vault(AGENT)["domains"][TEST_DOMAIN]

    marks = entry["approved_identity"]
    assert marks and all(len(m) == 64 for m in marks)
    assert _A_TOKEN not in repr(marks)
    assert marks == sorted(marks), "stored sorted, so two jars compare cheaply"


def test_a_jar_that_was_never_approved_gains_nothing_from_this(vault_home):
    """The check only ever removes an approval. It cannot create one — the
    defect this whole mechanism replaced was exactly a cookie write that
    could."""
    from dpc_client_core import web_auth

    web_auth.save_cookies(AGENT, TEST_DOMAIN, _account(_A_TOKEN))
    web_auth.save_cookies(AGENT, TEST_DOMAIN, _account(_A_TOKEN))

    assert web_auth.get_approval(AGENT, TEST_DOMAIN) is None


@pytest.mark.parametrize(
    "cookie, expected",
    [
        ({"name": "auth_token", "value": "a" * 40, "httponly": True}, True),
        ({"name": "ct0", "value": "a" * 40, "httponly": False}, False),
        ({"name": "lang", "value": "en", "httponly": True}, False),
        ({"name": "empty", "value": "", "httponly": True}, False),
        ({"name": "missing_value", "httponly": True}, False),
    ],
)
def test_what_counts_as_identity_bearing(cookie, expected):
    """The classifier is a property of the cookie, not a list of names —
    which is what lets it answer for a site nobody has thought about."""
    from dpc_client_core import web_auth

    assert web_auth._is_identity_bearing(cookie) is expected
