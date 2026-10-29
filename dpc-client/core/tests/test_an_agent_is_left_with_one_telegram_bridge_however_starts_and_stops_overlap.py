"""An agent ends with one Telegram bridge at most, whichever starts and stops overlap.

`DpcAgentManager.start()` builds the bridge last, behind the task processor and
the memory indexes, which can take minutes. A pasted image or a text to an agent
not yet started begins that start; enabling or re-linking its Telegram in the
window built a second bridge through TelegramService, and start() then built a
third over it. Each start built a bridge and then awaited its login, so the one
it replaced was never stopped: two pollers on one bot token, and Telegram
answers 409 Conflict.

Every start and stop of the bridge now takes one lock on the manager, and a start
stops the running bridge before it builds. The fake bridge here waits inside its
start until the test lets it go, and loses a stop that arrives while it waits,
as the real one does.

Nothing here touches the real ~/.dpc: `Path.home()` is a temporary directory,
and no Telegram is reached.
"""

import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.managers import agent_manager as am
from dpc_client_core.managers import agent_telegram_bridge
from dpc_client_core.telegram_service import TelegramService

AGENT = "agent_one_bridge_0001"
TOKEN = "123:abc"
CHAT = "41783586"


class _Bridge:
    """Polls from the moment its start finishes until stop() is called."""

    made = []
    gate = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.event_filter = []
        self._on_started = None
        self._retry_task = None
        self.running = False
        self.stopped = False
        _Bridge.made.append(self)

    def set_message_handler(self, handler, agent_manager):
        pass

    async def start(self):
        await _Bridge.gate.wait()
        # A stop that came while this waited is lost, as with the real bridge,
        # whose start sets _enabled after the login.
        self.running = True
        return True

    async def stop(self):
        self.stopped = True
        self.running = False


def _running():
    return [b for b in _Bridge.made if b.running]


class _Agent:
    """Stands in for DpcAgent: start() reaches the bridge through it unchanged."""

    def __init__(self, **kwargs):
        pass

    async def start_task_processor(self):
        pass

    def stop_task_processor(self):
        pass


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    registry = AgentRegistry()
    registry.register_agent(AGENT, "One-bridge test agent")
    registry.link_agent_to_telegram(AGENT, bot_token=TOKEN, chat_ids=[CHAT])

    _Bridge.made = []
    _Bridge.gate = asyncio.Event()
    monkeypatch.setattr(agent_telegram_bridge, "AgentTelegramBridge", _Bridge)
    monkeypatch.setattr(am, "DpcAgent", _Agent)

    service = SimpleNamespace(
        llm_manager=SimpleNamespace(providers={}),
        settings=None,
        telegram_manager=None,
        firewall=None,
    )
    mgr = am.DpcAgentManager(service, {}, agent_id=AGENT)

    # Hold start() where it spends its minutes: the memory indexes.
    mgr.indexes_gate = asyncio.Event()

    async def _init_memory_indexes():
        await mgr.indexes_gate.wait()

    monkeypatch.setattr(mgr, "_init_memory_indexes", _init_memory_indexes)
    return mgr


def _telegram_service(mgr, tmp_path):
    provider = SimpleNamespace(_managers={AGENT: mgr})
    return TelegramService(
        core_service_ref=None,
        settings=SimpleNamespace(get_telegram_config=lambda: {}),
        llm_manager=SimpleNamespace(providers={"dpc_agent": provider}),
        p2p_manager=None,
        dpc_home_dir=tmp_path,
    )


async def _let_the_tasks_run():
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_a_relink_while_the_agent_is_starting_leaves_one_bridge_running(manager, tmp_path):
    svc = _telegram_service(manager, tmp_path)

    start = asyncio.create_task(manager.start())
    await _let_the_tasks_run()
    assert manager._agent is not None and _Bridge.made == [], "start() is not held mid-way"

    relink = asyncio.create_task(svc.link_agent_telegram(AGENT, TOKEN, [CHAT]))
    await _let_the_tasks_run()
    assert len(_Bridge.made) == 1, "the relink did not reach the manager"

    manager.indexes_gate.set()  # start() goes on to its own bridge
    await _let_the_tasks_run()
    _Bridge.gate.set()
    result, _ = await asyncio.wait_for(asyncio.gather(relink, start), 5)

    assert result["status"] == "success"
    assert _running() == [manager._telegram_bridge]
    assert all(b.stopped for b in _Bridge.made if b is not manager._telegram_bridge)


@pytest.mark.asyncio
async def test_a_second_start_replaces_the_first_bridge(manager):
    first = asyncio.create_task(manager._start_telegram_bridge())
    await _let_the_tasks_run()
    second = asyncio.create_task(manager._start_telegram_bridge())
    await _let_the_tasks_run()

    _Bridge.gate.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert len(_Bridge.made) == 2
    one, two = _Bridge.made
    assert one.stopped and not one.running
    assert _running() == [two]
    assert manager._telegram_bridge is two


@pytest.mark.asyncio
async def test_a_stop_that_overlaps_a_start_leaves_no_bridge(manager):
    start = asyncio.create_task(manager._start_telegram_bridge())
    await _let_the_tasks_run()
    stop = asyncio.create_task(manager.stop())
    await _let_the_tasks_run()

    _Bridge.gate.set()
    await asyncio.wait_for(asyncio.gather(start, stop), 5)

    assert _running() == []
    assert manager._telegram_bridge is None


@pytest.mark.asyncio
async def test_a_stop_while_start_is_on_its_indexes_leaves_no_bridge(manager):
    start = asyncio.create_task(manager.start())
    await _let_the_tasks_run()

    await asyncio.wait_for(manager.stop(), 5)
    manager.indexes_gate.set()
    _Bridge.gate.set()
    await asyncio.wait_for(asyncio.gather(start, return_exceptions=True), 5)

    assert _running() == []
    assert manager._telegram_bridge is None


@pytest.mark.asyncio
async def test_a_start_when_telegram_is_off_stops_the_running_bridge_and_builds_none(manager):
    _Bridge.gate.set()
    await manager._start_telegram_bridge()
    (running,) = _running()

    AgentRegistry().set_agent_telegram_enabled(AGENT, False)
    await manager._start_telegram_bridge()

    assert running.stopped
    assert _running() == []
    assert manager._telegram_bridge is None
    assert len(_Bridge.made) == 1
