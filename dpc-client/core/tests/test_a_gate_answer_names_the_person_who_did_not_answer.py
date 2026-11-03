"""The two answers a tier-1 command can get from nobody or from a refusal name the owner.

"Command rejected by user" and "approval timed out" read like a machine
verdict. The agent reads them as the result of its command; the owner's name
tells it a person decided, or was elsewhere, and that retrying the same command
in a loop is not what is wanted. The name is the node owner's display name,
the one the service signs the owner's chat messages with, "User" when none is set.
"""
import asyncio
import threading
import types

import pytest

from dpc_client_core.dpc_agent.tools import shell
from dpc_client_core.service import CoreService


@pytest.fixture(autouse=True)
def _no_leftover_requests():
    shell._pending_approvals.clear()
    yield
    shell._pending_approvals.clear()


class _Service:
    def __init__(self, name):
        self.local_api = types.SimpleNamespace(has_clients=True)
        self.p2p_manager = types.SimpleNamespace(get_display_name=lambda: name)

    async def announce_shell_approval_request(self, **kwargs):
        pass

    async def announce_shell_approval_closed(self, **kwargs):
        pass


def _ctx(tmp_path, name):
    return types.SimpleNamespace(
        agent_root=tmp_path, dpc_service=_Service(name),
        reply_telegram_chat_id="", _event_loop=None, _agent=None,
    )


def test_a_timeout_says_the_owner_is_not_focused_on_the_task(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 0.05)
    result = shell._request_approval(_ctx(tmp_path, "Mike"), "rm -rf x", "tier1", "", 10)
    assert result.startswith("⏳ Command approval timed out after 0.05s: ")
    assert "User (Mike) is not focused on current task now" in result
    assert "rm -rf x" in result  # the agent still learns which command


def test_a_timeout_with_no_display_name_says_user(tmp_path, monkeypatch):
    monkeypatch.setattr(shell, "APPROVAL_TTL_SECONDS", 0.05)
    result = shell._request_approval(_ctx(tmp_path, None), "rm -rf x", "tier1", "", 10)
    assert "User (User) is not focused" not in result
    assert "User is not focused on current task now" in result


def test_a_refusal_from_the_service_reaches_the_agent_with_the_owner_name(tmp_path):
    loop = asyncio.new_event_loop()
    runner = threading.Thread(target=loop.run_forever, daemon=True)
    runner.start()
    try:
        ctx = _ctx(tmp_path, "Mike")
        ctx._event_loop = loop
        service = types.SimpleNamespace(
            p2p_manager=ctx.dpc_service.p2p_manager,
            announce_shell_approval_closed=ctx.dpc_service.announce_shell_approval_closed,
        )
        out = {}
        t = threading.Thread(
            target=lambda: out.update(r=shell._request_approval(ctx, "rm -rf x", "tier1", "", 10)))
        t.start()
        for _ in range(200):
            if shell._pending_approvals:
                break
            threading.Event().wait(0.01)
        (request_id,) = list(shell._pending_approvals)
        asyncio.run_coroutine_threadsafe(
            CoreService.shell_reject_command(service, request_id), loop).result(5)
        t.join(5)
        assert out["r"] == "Command manually rejected by Mike"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        runner.join(timeout=5)
        loop.close()


def test_a_refusal_with_no_display_name_says_user(tmp_path):
    entry = {"event": threading.Event(), "agent_id": ""}
    shell._pending_approvals["r1"] = entry
    service = types.SimpleNamespace(
        p2p_manager=types.SimpleNamespace(get_display_name=lambda: ""),
        announce_shell_approval_closed=_Service("x").announce_shell_approval_closed,
    )
    asyncio.run(CoreService.shell_reject_command(service, "r1"))
    assert entry["result"] == "Command manually rejected by User"
