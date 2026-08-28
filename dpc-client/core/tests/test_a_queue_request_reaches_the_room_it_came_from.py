"""A check_back asked for from Telegram was decided by a card on a desktop.

`_await_schedule_approval` had one delivery — a broadcast to the interface —
while the shell gate beside it has offered its question in the chat the run came
from since ADR-030 v2. Measured 2026-08-27: Mike was working from Telegram, the
desktop was open but unattended, and sixty seconds later the agent was told
«nobody answered within 60s» and reported a refusal.

The field the fix needed was already in scope ten lines below the gate:
`reply_telegram_chat_id`, injected into the task data so the *result* returns to
the right chat. The result knew how to go home; the question did not ask.
"""
import asyncio
import types

import pytest

from dpc_client_core.dpc_agent.tools import core as tools_core


class _LocalApi:
    def __init__(self, attached: bool):
        self.has_clients = attached
        self.events = []

    async def broadcast_event(self, name, payload):
        self.events.append((name, payload))


class _Service:
    """Only what the gate touches, and it records what it was asked to say."""

    def __init__(self, attached: bool):
        self.local_api = _LocalApi(attached)
        self.requests = []
        self.closures = []

    async def announce_schedule_approval_request(self, **kwargs):
        self.requests.append(kwargs)

    async def announce_schedule_approval_closed(self, **kwargs):
        self.closures.append(kwargs)


def _ctx(service, *, telegram_chat_id="", loop=None):
    return types.SimpleNamespace(
        dpc_service=service,
        _event_loop=loop,
        reply_telegram_chat_id=telegram_chat_id,
        current_task_id="task-1",
        agent_root=types.SimpleNamespace(name="agent_iris_63f1b6bf"),
        _agent=None,
    )


@pytest.fixture
def quick_ttl(monkeypatch):
    """A minute is the production wait; a fifth of a second is the same code."""
    monkeypatch.setattr(tools_core, "_SCHEDULE_APPROVAL_TTL_SECONDS", 0.2)
    return 0.2


async def _ask(ctx, **kwargs):
    """Run the blocking gate off the loop, the way the executor thread does."""
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: tools_core._await_schedule_approval(
            ctx, task_type="check_back", when="in 1200s", about="remind Mike", **kwargs
        ),
    )


class TestWhoCanBeAsked:
    @pytest.mark.asyncio
    async def test_no_interface_and_no_telegram_is_refused_before_anything_is_offered(self):
        service = _Service(attached=False)
        ctx = _ctx(service, loop=asyncio.get_running_loop())

        approved, reason = await _ask(ctx)

        assert approved is False
        assert "did not come from Telegram" in reason
        assert service.requests == [], "nothing should be offered when nobody can answer"

    @pytest.mark.asyncio
    async def test_telegram_alone_is_enough_to_ask(self, quick_ttl):
        """The case that used to fail: the desktop is shut, the chat is open."""
        service = _Service(attached=False)
        ctx = _ctx(service, telegram_chat_id="429727247", loop=asyncio.get_running_loop())

        approved, reason = await _ask(ctx)

        assert service.requests, "the question must reach the room the run came from"
        assert approved is False and "nobody answered" in reason  # nobody pressed it

    @pytest.mark.asyncio
    async def test_the_offer_carries_the_chat_and_the_deadline(self, quick_ttl):
        service = _Service(attached=True)
        ctx = _ctx(service, telegram_chat_id="429727247", loop=asyncio.get_running_loop())

        await _ask(ctx)

        offer = service.requests[0]
        assert offer["telegram_chat_id"] == "429727247"
        assert offer["timeout_seconds"] == quick_ttl, "the card needs a deadline to retire on"
        assert offer["agent_id"] == "agent_iris_63f1b6bf"
        assert offer["about"] == "remind Mike"


class TestWhatHappensWhenNobodyAnswers:
    @pytest.mark.asyncio
    async def test_the_request_is_withdrawn_from_the_surfaces_still_showing_it(self, quick_ttl):
        service = _Service(attached=True)
        ctx = _ctx(service, telegram_chat_id="429727247", loop=asyncio.get_running_loop())

        await _ask(ctx)
        await asyncio.sleep(0)  # let the closure coroutine run on this loop

        assert service.closures, "an expired request must stop being offered"
        assert service.closures[0]["resolution"] == "expired"
        assert service.closures[0]["agent_id"] == "agent_iris_63f1b6bf"


class TestAnsweringIt:
    @pytest.mark.asyncio
    async def test_a_press_is_credited_to_the_agent_that_asked(self, quick_ttl):
        """The id is read before the entry is consumed, so a closure can find
        the bridge the question was offered on."""
        service = _Service(attached=True)
        ctx = _ctx(service, loop=asyncio.get_running_loop())

        task = asyncio.ensure_future(_ask(ctx))
        for _ in range(50):
            await asyncio.sleep(0.002)
            if service.requests:
                break
        request_id = service.requests[0]["request_id"]

        assert tools_core.pending_schedule_agent_id(request_id) == "agent_iris_63f1b6bf"
        assert tools_core.resolve_schedule_approval(request_id, True) is True

        approved, reason = await task
        assert (approved, reason) == (True, "")

    @pytest.mark.asyncio
    async def test_an_id_nobody_holds_is_credited_to_nobody(self):
        assert tools_core.pending_schedule_agent_id("never-issued") == ""
