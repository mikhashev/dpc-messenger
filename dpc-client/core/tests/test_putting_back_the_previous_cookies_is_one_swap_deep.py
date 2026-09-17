"""The copy kept behind a replaced jar is reachable by a person, not only by
a test.

`restore_previous_cookies` had existed since the jar-keeping was written and
had no caller outside the test suite. Insurance nobody can reach is not
insurance: the writeback is unconditional, so the kept copy is what stands
between a window that stored the wrong thing and a login that is gone.

It is a swap, not a pop — the jar being undone becomes the kept one — which
is the whole of what the person needs told: pressing it twice returns them
where they started, and there is nothing older than the two.

What is pinned here:
- the command is on the UI's allowlist and is a coroutine on CoreService,
  which are the dispatcher's two conditions;
- it swaps, and swaps back;
- a site with nothing kept answers `restored: False` rather than claiming a
  restore;
- both outcomes leave an audit row;
- the answer says the swap is reversible and one generation deep, because a
  person who reads it as an undo history will expect a second step back.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dpc_client_core import local_api
from .conftest import TEST_DOMAIN
from .test_web_audit import vault_home  # noqa: F401


def _cookies(name: str, domain: str = TEST_DOMAIN) -> list[dict]:
    return [{
        "name": name, "value": "v", "domain": f".{domain}", "path": "/",
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


async def _restore(agent_id: str, domain: str):
    from dpc_client_core import service as service_module

    return await service_module.CoreService.web_auth_restore_previous_cookies(
        _service(), agent_id=agent_id, domain=domain,
    )


def _names(agent_id: str, domain: str = TEST_DOMAIN) -> list[str]:
    from dpc_client_core import web_auth

    return [c["name"] for c in web_auth.load_cookies(agent_id, domain) or []]


def _two_generations(agent_id: str = "agent_a") -> None:
    from dpc_client_core import web_auth

    web_auth.save_cookies(agent_id, TEST_DOMAIN, _cookies("the_real_one"))
    web_auth.save_cookies(agent_id, TEST_DOMAIN, _cookies("the_wrong_one"))


def test_the_command_is_reachable_from_the_ui():
    """The dispatcher wants both halves: a name on the allowlist and a
    coroutine on CoreService under exactly that name."""
    import asyncio

    from dpc_client_core.service import CoreService

    assert "web_auth_restore_previous_cookies" in local_api.ALLOWED_COMMANDS
    assert asyncio.iscoroutinefunction(
        CoreService.web_auth_restore_previous_cookies
    )


@pytest.mark.asyncio
async def test_the_restore_puts_back_what_the_last_write_displaced(vault_home):  # noqa: F811
    _two_generations()
    assert _names("agent_a") == ["the_wrong_one"]

    result = await _restore("agent_a", TEST_DOMAIN)

    assert result["status"] == "success"
    assert result["restored"] is True
    assert result["domain"] == TEST_DOMAIN
    assert _names("agent_a") == ["the_real_one"]


@pytest.mark.asyncio
async def test_pressing_it_twice_returns_where_it_started(vault_home):  # noqa: F811
    """The answer says so, so the behaviour has to hold — a person who
    restores the wrong generation gets out of it the same way they got in."""
    _two_generations()

    await _restore("agent_a", TEST_DOMAIN)
    second = await _restore("agent_a", TEST_DOMAIN)

    assert second["restored"] is True
    assert _names("agent_a") == ["the_wrong_one"]


@pytest.mark.asyncio
async def test_a_site_with_nothing_kept_is_a_truthful_no_op(vault_home):  # noqa: F811
    from dpc_client_core import web_auth

    web_auth.save_cookies("agent_a", TEST_DOMAIN, _cookies("only_one"))

    result = await _restore("agent_a", TEST_DOMAIN)

    assert result["status"] == "success"
    assert result["restored"] is False
    assert "Nothing was changed" in result["message"]
    assert _names("agent_a") == ["only_one"]


@pytest.mark.asyncio
async def test_the_answer_says_it_is_a_swap_and_one_deep(vault_home):  # noqa: F811
    """Read as an undo history this control disappoints twice: the second
    press looks like a bug, and the person expects a third step that is not
    there."""
    _two_generations()

    message = (await _restore("agent_a", TEST_DOMAIN))["message"]

    assert "Swapped" in message
    assert "again" in message
    assert "one earlier set is kept" in message


@pytest.mark.asyncio
async def test_both_outcomes_are_audited(vault_home):  # noqa: F811
    _two_generations()

    await _restore("agent_a", TEST_DOMAIN)
    assert [r["status"] for r in _audit(vault_home, "agent_a")] == [
        "web_auth_restored"
    ]

    from dpc_client_core import web_auth

    web_auth.forget_cookies("agent_a", TEST_DOMAIN)
    web_auth.save_cookies("agent_a", TEST_DOMAIN, _cookies("only_one"))
    await _restore("agent_a", TEST_DOMAIN)

    assert [r["status"] for r in _audit(vault_home, "agent_a")] == [
        "web_auth_restored", "web_auth_restore_nothing_kept",
    ]


@pytest.mark.asyncio
async def test_a_subdomain_addresses_the_jar_that_exists(vault_home):  # noqa: F811
    """Jars are filed under the eTLD+1, so the input is resolved before the
    lookup — otherwise the control could miss the row that rendered it."""
    _two_generations()

    result = await _restore("agent_a", f"mail.{TEST_DOMAIN}")

    assert result["restored"] is True
    assert result["domain"] == TEST_DOMAIN


@pytest.mark.asyncio
async def test_an_input_that_names_no_jar_is_an_error_not_a_success(vault_home):  # noqa: F811
    result = await _restore("agent_a", "com")

    assert result["status"] == "error"
    assert "registrable domain" in result["message"]


@pytest.mark.asyncio
async def test_the_agent_id_is_refused_before_it_becomes_a_path(vault_home):  # noqa: F811
    """The command writes under `~/.dpc/agents/<id>/` — the audit row if
    nothing else — so the id meets the same rule as the rest of the vault
    surface."""
    for bad in ["", "..", "a/b", "ext:CC"]:
        assert (await _restore(bad, TEST_DOMAIN))["status"] == "error"


@pytest.mark.asyncio
async def test_restoring_one_site_leaves_the_others_alone(vault_home):  # noqa: F811
    other = "example.com" if TEST_DOMAIN != "example.com" else "example.net"
    from dpc_client_core import web_auth

    _two_generations()
    web_auth.save_cookies("agent_a", other, _cookies("untouched", other))

    await _restore("agent_a", TEST_DOMAIN)

    assert _names("agent_a", other) == ["untouched"]


def test_no_agent_tool_restores_stored_cookies():
    """Which set of cookies an agent carries is the person's decision, as is
    taking it back. A tool here would hand the agent the switch."""
    from dpc_client_core.dpc_agent.tools import web_auth_tools

    assert [t.name for t in web_auth_tools.get_tools()] == ["list_auth_domains"]
