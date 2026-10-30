"""A desktop Stop on an idle agent drops the Telegram messages its bot is gathering.

While the agent runs nothing and a burst sits in its quiet window, nothing is
at the run gate yet, so interrupt_agent never reached the manager and the
gathered messages still went to the agent afterwards. The Stop now drops them
after stop_waiting (Mike's call, 2026-10-30).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dpc_client_core.service import CoreService


class _Manager:
    def __init__(self, dropped: int):
        self.dropped = dropped
        self.drops = 0

    def has_active_loop(self, conversation_id):
        return False

    def interrupt(self, conversation_id, kill=False):
        raise AssertionError("no loop runs, interrupt must not be called")

    def drop_telegram_bursts(self):
        self.drops += 1
        return self.dropped


def _service(manager):
    svc = CoreService.__new__(CoreService)
    provider = SimpleNamespace(_managers={"agent_001": manager}, get_manager=lambda aid: manager)
    svc.llm_manager = SimpleNamespace(providers={"dpc_agent": provider})
    return svc


@pytest.mark.asyncio
async def test_a_stop_on_an_idle_agent_drops_what_its_bot_gathered():
    manager = _Manager(dropped=2)

    result = await _service(manager).interrupt_agent(agent_id="agent_001", conversation_id="agent_001")

    assert manager.drops == 1
    assert result == {"status": "stopped", "agent_id": "agent_001", "agent_ids": ["agent_001"]}


@pytest.mark.asyncio
async def test_a_stop_with_nothing_gathered_still_says_nothing_ran():
    manager = _Manager(dropped=0)

    result = await _service(manager).interrupt_agent(agent_id="agent_001", conversation_id="agent_001")

    assert manager.drops == 1
    assert result == {"status": "no_active_loop"}
