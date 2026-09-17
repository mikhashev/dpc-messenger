"""Deleting stored cookies is a command, the command is the human's, and it
is not a logout.

The button used to say "Revoke", which reads as taking access away from the
account. It never did that: the session on the site stays valid until the
person signs out there, in the visible window. What it deletes is D-PC's copy,
which is a real and separate thing to want — it is the only way to take a login
away from an agent without touching the site — so the capability stays and the
name stops overclaiming (Mike's call, 2026-09-11).

What is pinned here:
- the read is `web_auth_list_domains`, the write is `web_auth_forget_cookies`,
  both on the UI's allowlist and neither in the agent's tool registry;
- forgetting a site with no jar answers `forgotten: False` rather than claiming
  a deletion;
- both outcomes leave an audit row;
- forgetting one site leaves the agent's other jars untouched;
- the message cannot be read as "you are signed out of the account".
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dpc_client_core import local_api
from .conftest import TEST_DOMAIN
# The vault fixture (temp DPC_HOME + in-memory keyring) lives beside the audit
# tests; importing it is how pytest shares a fixture across modules.
from .test_web_audit import vault_home  # noqa: F401

OTHER_DOMAIN = "example.com" if TEST_DOMAIN != "example.com" else "example.net"


def _cookies(domain: str) -> list[dict]:
    return [{
        "name": "s", "value": "v", "domain": f".{domain}", "path": "/",
        "expires": int(time.time()) + 3600, "secure": True, "httponly": True,
        "samesite": "Lax",
    }]


def _audit(home: Path, agent_id: str) -> list[dict]:
    path = home / "agents" / agent_id / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _service():
    from dpc_client_core import service as service_module

    return service_module.CoreService.__new__(service_module.CoreService)


async def _forget(agent_id: str, domain: str):
    from dpc_client_core import service as service_module

    return await service_module.CoreService.web_auth_forget_cookies(
        _service(), agent_id=agent_id, domain=domain,
    )


async def _list(agent_id: str):
    from dpc_client_core import service as service_module

    return await service_module.CoreService.web_auth_list_domains(
        _service(), agent_id=agent_id,
    )


def _jar(agent_id: str, domain: str) -> None:
    from dpc_client_core import web_auth

    web_auth.save_cookies(agent_id, domain, _cookies(domain))


def test_both_commands_are_reachable_from_the_ui():
    """An unlisted command is invisible however well it works — and the person
    cannot delete what they cannot see, so the read is half the feature.

    The allowlist is only one of the dispatcher's two conditions; it also
    requires a coroutine on CoreService under exactly that name, and answers
    "Unknown or non-async command" when either half is missing.
    """
    import asyncio

    from dpc_client_core.service import CoreService

    for command in ("web_auth_list_domains", "web_auth_forget_cookies"):
        assert command in local_api.ALLOWED_COMMANDS
        assert asyncio.iscoroutinefunction(getattr(CoreService, command))


@pytest.mark.asyncio
async def test_forgetting_a_site_leaves_no_jar_behind(vault_home):  # noqa: F811
    from dpc_client_core import web_auth

    _jar("agent_a", TEST_DOMAIN)

    result = await _forget("agent_a", TEST_DOMAIN)

    assert result["status"] == "success"
    assert result["forgotten"] is True
    assert result["domain"] == TEST_DOMAIN
    assert web_auth.load_cookies("agent_a", TEST_DOMAIN) is None
    assert web_auth.list_domains("agent_a") == []


@pytest.mark.asyncio
async def test_the_answer_does_not_claim_to_have_signed_anybody_out(vault_home):  # noqa: F811
    """The whole reason the name changed. A person who reads this as "logged
    out of the account" leaves a live session running and believes otherwise."""
    _jar("agent_a", TEST_DOMAIN)

    message = (await _forget("agent_a", TEST_DOMAIN))["message"]

    assert "still signed in" in message
    assert "sign out there" in message
    assert "revoke" not in message.lower()


@pytest.mark.asyncio
async def test_forgetting_a_site_with_no_jar_is_a_truthful_no_op(vault_home):  # noqa: F811
    """The end state is the same as a real deletion, which is exactly why it
    has to say which one happened."""
    result = await _forget("agent_a", TEST_DOMAIN)

    assert result["status"] == "success"
    assert result["forgotten"] is False
    assert "nothing to forget" in result["message"]


@pytest.mark.asyncio
async def test_the_deletion_is_audited(vault_home):  # noqa: F811
    """Both outcomes, distinguishably. An audit that only records arrivals can
    show a credential arriving and never show it leaving."""
    _jar("agent_a", TEST_DOMAIN)

    await _forget("agent_a", TEST_DOMAIN)
    rows = _audit(vault_home, "agent_a")
    assert [r["status"] for r in rows] == ["web_auth_forgotten"]
    assert rows[0]["agent_id"] == "agent_a"
    assert rows[0]["domain"] == TEST_DOMAIN
    assert rows[0]["timestamp"]

    await _forget("agent_a", TEST_DOMAIN)
    statuses = [r["status"] for r in _audit(vault_home, "agent_a")]
    assert statuses == ["web_auth_forgotten", "web_auth_forget_no_jar"]


@pytest.mark.asyncio
async def test_forgetting_one_site_leaves_the_others_intact(vault_home):  # noqa: F811
    """One Fernet key encrypts every domain in the vault, so a deletion that
    reached the key would empty the agent's whole vault at once."""
    from dpc_client_core import web_auth

    _jar("agent_a", TEST_DOMAIN)
    _jar("agent_a", OTHER_DOMAIN)

    await _forget("agent_a", TEST_DOMAIN)

    assert [row["domain"] for row in web_auth.list_domains("agent_a")] == [OTHER_DOMAIN]
    assert web_auth.load_cookies("agent_a", OTHER_DOMAIN) == _cookies(OTHER_DOMAIN)


@pytest.mark.asyncio
async def test_a_subdomain_addresses_the_jar_that_exists(vault_home):  # noqa: F811
    """Jars are filed under the eTLD+1, so the input is resolved before the
    lookup — otherwise Forget on a row the panel itself rendered could miss."""
    from dpc_client_core import web_auth

    _jar("agent_a", TEST_DOMAIN)

    result = await _forget("agent_a", f"mail.{TEST_DOMAIN}")

    assert result["forgotten"] is True
    assert result["domain"] == TEST_DOMAIN
    assert web_auth.list_domains("agent_a") == []


@pytest.mark.asyncio
async def test_an_input_that_names_no_jar_is_an_error_not_a_success(vault_home):  # noqa: F811
    """`com` addresses no jar. Answering "forgotten" for it would tell the
    person cookies were deleted when none were."""
    result = await _forget("agent_a", "com")

    assert result["status"] == "error"
    assert "registrable domain" in result["message"]


@pytest.mark.asyncio
async def test_the_agent_id_is_refused_before_it_becomes_a_path(vault_home):  # noqa: F811
    """Both commands write under `~/.dpc/agents/<id>/` — the audit row if
    nothing else — so the id is checked against the same rule that guards
    `get_agent_root`."""
    for bad in ["", "..", "a/b", "ext:CC"]:
        assert (await _forget(bad, TEST_DOMAIN))["status"] == "error"
        assert (await _list(bad))["status"] == "error"


@pytest.mark.asyncio
async def test_the_listing_says_which_sites_have_cookies(vault_home):  # noqa: F811
    """The listing is the half worth keeping: without it nobody can see what
    the agent is carrying."""
    _jar("agent_a", TEST_DOMAIN)

    result = await _list("agent_a")

    assert result["status"] == "success"
    assert result["agent_id"] == "agent_a"
    row = result["domains"][0]
    assert row["domain"] == TEST_DOMAIN
    assert row["has_cookies"] is True
    assert "approved" not in row


@pytest.mark.asyncio
async def test_the_listing_does_not_touch_last_used_at(vault_home):  # noqa: F811
    """`load_cookies` stamps it; opening a panel must not, or the column that
    says when the agent last spent a login would report the person reading it.

    Read off the vault rather than off the answer: a listing that stamps as it
    goes still returns the pre-stamp snapshot, so the returned row cannot see
    the write. Measured — that is how the first version of this test passed
    under the mutation it is named for.
    """
    from dpc_client_core import web_auth

    _jar("agent_a", TEST_DOMAIN)
    before = web_auth.list_domains("agent_a")[0]["last_used_at"]

    time.sleep(1.1)  # the vault stamps to whole seconds
    await _list("agent_a")

    assert web_auth.list_domains("agent_a")[0]["last_used_at"] == before


def test_no_agent_tool_deletes_stored_cookies():
    """The decision to spend a login is the person's, and so is the decision to
    take it back. A tool here would hand the agent the eraser."""
    from dpc_client_core.dpc_agent.tools import web_auth_tools

    assert [t.name for t in web_auth_tools.get_tools()] == ["list_auth_domains"]
