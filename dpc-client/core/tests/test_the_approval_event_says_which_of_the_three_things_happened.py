"""A closed approval says whether it was approved, rejected or timed out.

On 2026-08-25 Mike asked a question the logs should have answered in a line —
did Johnny keep hitting the approval wall, or was nobody at the keyboard? —
and sent me to `ui.log`. It held **35** `Shell approval request` lines and
**35** `Shell approval expired` lines, and nothing else: no approvals at all.

The backend log said otherwise. Of those 35, **30 were approved**, five timed
out at exactly 60.0 s, none rejected; the approval wait had a median of 3.0 s.

The cause was one method. `announce_shell_approval_closed` broadcast
`shell_approval_expired` for **every** closure — its callers are approve
(«✅ Approved elsewhere»), reject («❌ Rejected elsewhere»), the TTL timeout
and the superseded case. The Telegram bridge received the `outcome` string and
could tell them apart. The UI received a name that said «expired», so the only
surface a person opens was wrong about 30 of 35 events, always in the
direction that makes the gate look like a wall.

What is pinned here is not that an event fires — the old code fired one too —
but that **the three outcomes are distinguishable at the surface**.
"""

import asyncio
import types

import pytest


class FakeLocalApi:
    def __init__(self):
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


def _service():
    from dpc_client_core.service import CoreService

    service = CoreService.__new__(CoreService)
    service.local_api = FakeLocalApi()
    service._get_agent_telegram_bridge = lambda agent_id: None
    return service


def _resolved(service):
    return [p for n, p in service.local_api.events if n == "shell_approval_resolved"]


def _withdrawals(service):
    return [p for n, p in service.local_api.events if n == "shell_approval_expired"]


# --- the four outcomes are four answers --------------------------------------

@pytest.mark.parametrize("resolution", ["approved", "rejected", "expired", "superseded"])
def test_each_outcome_reports_itself(resolution):
    service = _service()

    asyncio.run(service.announce_shell_approval_closed(
        request_id="abc123", agent_id="agent_a",
        outcome="whatever a person reads", resolution=resolution,
    ))

    payloads = _resolved(service)
    assert len(payloads) == 1
    assert payloads[0]["resolution"] == resolution
    assert payloads[0]["request_id"] == "abc123"


def test_an_approval_does_not_read_as_a_timeout():
    """The whole defect in one assertion."""
    service = _service()

    asyncio.run(service.announce_shell_approval_closed(
        request_id="abc123", agent_id="agent_a",
        outcome="✅ Approved elsewhere.", resolution="approved",
    ))

    assert _resolved(service)[0]["resolution"] == "approved"


def test_the_human_string_travels_with_the_machine_one():
    """A UI that wants to show something can, without a lookup table here."""
    service = _service()

    asyncio.run(service.announce_shell_approval_closed(
        request_id="abc123", agent_id="agent_a",
        outcome="❌ Rejected elsewhere.", resolution="rejected",
    ))

    assert _resolved(service)[0]["outcome"] == "❌ Rejected elsewhere."


# --- the old event stays, and stays meaningless -------------------------------

def test_the_withdrawal_event_is_still_sent_for_an_older_ui():
    """`shell_approval_expired` now means only «stop showing this card». It is
    kept so a UI predating this change still withdraws it — dropping it would
    leave a dead button on every approval."""
    service = _service()

    asyncio.run(service.announce_shell_approval_closed(
        request_id="abc123", agent_id="agent_a",
        outcome="✅ Approved elsewhere.", resolution="approved",
    ))

    assert len(_withdrawals(service)) == 1
    assert _withdrawals(service)[0] == {"request_id": "abc123"}


def test_the_truthful_event_is_sent_before_the_ambiguous_one():
    """Order matters for the log: whichever a reader sees first sets their
    expectation, and the old line is what sent an hour of reading wrong."""
    service = _service()

    asyncio.run(service.announce_shell_approval_closed(
        request_id="abc123", agent_id="agent_a",
        outcome="✅ Approved elsewhere.", resolution="approved",
    ))

    names = [n for n, _ in service.local_api.events]
    assert names == ["shell_approval_resolved", "shell_approval_expired"]


# --- the real call sites pass the real resolution ----------------------------

def test_approving_a_command_announces_an_approval():
    """A parameter with a default is only as good as its call sites — the
    default here is `expired`, so a caller that forgets to pass one reproduces
    the original defect silently."""
    from dpc_client_core.dpc_agent.tools import shell as shell_tool

    service = _service()
    entry = {
        "command": "echo hi", "agent_name": "Johnny", "agent_id": "agent_a",
        "event": types.SimpleNamespace(set=lambda: None),
    }
    shell_tool._pending_approvals["req1"] = entry
    try:
        asyncio.run(service.shell_approve_command("req1"))
    finally:
        shell_tool._pending_approvals.pop("req1", None)

    assert _resolved(service)[0]["resolution"] == "approved"


def test_rejecting_a_command_announces_a_rejection():
    from dpc_client_core.dpc_agent.tools import shell as shell_tool

    service = _service()
    shell_tool._pending_approvals["req2"] = {
        "command": "echo hi", "agent_name": "Johnny", "agent_id": "agent_a",
        "event": types.SimpleNamespace(set=lambda: None),
    }
    try:
        asyncio.run(service.shell_reject_command("req2"))
    finally:
        shell_tool._pending_approvals.pop("req2", None)

    assert _resolved(service)[0]["resolution"] == "rejected"


def test_every_call_site_passes_a_resolution():
    """Counted rather than trusted: the default is `expired`, so a call site
    that omits it turns an approval back into a reported timeout."""
    import ast
    import inspect
    from pathlib import Path

    from dpc_client_core import service as service_mod
    from dpc_client_core.dpc_agent.tools import shell as shell_mod

    missing = []
    for module in (service_mod, shell_mod):
        tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "announce_shell_approval_closed":
                continue
            if not any(kw.arg == "resolution" for kw in node.keywords):
                missing.append(f"{Path(inspect.getfile(module)).name}:{node.lineno}")

    assert not missing, f"call sites without an explicit resolution: {missing}"
