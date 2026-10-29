"""A command marked slow runs as a task, so two presses of one button can overlap.

Before the mark, the read loop awaited each handler inline and two presses
reached the backend one after the other. Four marked handlers are not safe when
they overlap with themselves:

- the three that start or stop an agent's Telegram bridge. A start builds the
  bridge and then awaits it, so two starts leave one bridge polling with no
  owner, and two pollers on one bot token make Telegram answer 409 Conflict. A
  start that overlaps a stop can leave a bridge running for a link marked
  disabled.
- the Hub login. The token is set only when the OAuth flow ends, so a second
  press passes the "already authenticated" check, starts a second flow and a
  second callback server on the same port.

Each takes a lock of its own. These tests drive two calls at once through a stub
that stays inside the start (or the OAuth flow) until the test lets it go.
"""

import asyncio
import io
import logging
import threading
from types import SimpleNamespace

import pytest

from dpc_client_core import hub_client as hub_client_module
from dpc_client_core.dpc_agent.utils import AgentRegistry
from dpc_client_core.hub_client import HubClient
from dpc_client_core.telegram_service import TelegramService

AGENT = "agent_ark_0001"

PAUSED = {
    "agent_id": AGENT,
    "telegram_enabled": False,
    "telegram_bot_token": "123:abc",
    "telegram_allowed_chat_ids": ["41783586"],
}


# --- the Telegram bridge ----------------------------------------------------


class _Bridge:
    """Alive from the moment its start finishes until stop() is called.

    A stop that arrives while the start is still waiting is lost, as with the
    real bridge: the start finishes afterwards and the bridge runs.
    """

    def __init__(self, manager):
        self.manager = manager
        self.running = False

    async def start(self):
        await self.manager.gate.wait()
        self.running = True

    async def stop(self):
        self.running = False


class _Manager:
    """Builds a bridge and only then awaits it, as DpcAgentManager does."""

    def __init__(self):
        self.gate = asyncio.Event()
        self._telegram_bridge = None
        self.bridges = []
        self.active = 0
        self.max_active = 0

    async def _start_telegram_bridge(self):
        bridge = _Bridge(self)
        self._telegram_bridge = bridge
        self.bridges.append(bridge)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await bridge.start()
        finally:
            self.active -= 1

    def running(self):
        return [b for b in self.bridges if b.running]


def _registry(agent):
    reg = AgentRegistry.__new__(AgentRegistry)

    def _update(agent_id, updates):
        agent.update(updates)
        return agent

    reg.get_agent = lambda agent_id: agent if agent_id == AGENT else None
    reg.update_agent = _update
    reg.unlink_agent_from_telegram = lambda agent_id: agent.update(
        {"telegram_enabled": False, "telegram_bot_token": None}
    )
    return reg


def _service(tmp_path):
    manager = _Manager()
    provider = SimpleNamespace(_managers={AGENT: manager})
    svc = TelegramService(
        core_service_ref=None,
        settings=SimpleNamespace(get_telegram_config=lambda: {}),
        llm_manager=SimpleNamespace(providers={"dpc_agent": provider}),
        p2p_manager=None,
        dpc_home_dir=tmp_path,
    )
    return svc, manager


@pytest.fixture
def agent_and_service(tmp_path, monkeypatch):
    agent = dict(PAUSED)
    reg = _registry(agent)
    monkeypatch.setattr(
        "dpc_client_core.dpc_agent.utils.AgentRegistry", lambda *a, **kw: reg
    )
    svc, manager = _service(tmp_path)
    return agent, svc, manager


async def _let_the_tasks_run():
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_two_enables_at_once_start_one_bridge_after_the_other(agent_and_service):
    agent, svc, manager = agent_and_service

    first = asyncio.create_task(svc.set_agent_telegram_enabled(AGENT, True))
    second = asyncio.create_task(svc.set_agent_telegram_enabled(AGENT, True))
    await _let_the_tasks_run()
    assert manager.max_active == 1, "a second start began while the first was running"

    manager.gate.set()
    results = await asyncio.wait_for(asyncio.gather(first, second), 5)

    assert [r["status"] for r in results] == ["success", "success"]
    assert manager.max_active == 1
    assert len(manager.running()) == 1
    assert agent["telegram_enabled"] is True


@pytest.mark.asyncio
async def test_a_disable_pressed_after_an_enable_ends_disabled_with_no_bridge(agent_and_service):
    agent, svc, manager = agent_and_service

    enable = asyncio.create_task(svc.set_agent_telegram_enabled(AGENT, True))
    disable = asyncio.create_task(svc.set_agent_telegram_enabled(AGENT, False))
    await _let_the_tasks_run()

    manager.gate.set()
    await asyncio.wait_for(asyncio.gather(enable, disable), 5)

    assert agent["telegram_enabled"] is False
    assert manager.running() == []
    assert manager._telegram_bridge is None


@pytest.mark.asyncio
async def test_an_unlink_pressed_after_an_enable_leaves_no_bridge(agent_and_service):
    agent, svc, manager = agent_and_service

    enable = asyncio.create_task(svc.set_agent_telegram_enabled(AGENT, True))
    unlink = asyncio.create_task(svc.unlink_agent_telegram(AGENT))
    await _let_the_tasks_run()

    manager.gate.set()
    await asyncio.wait_for(asyncio.gather(enable, unlink), 5)

    assert agent["telegram_enabled"] is False
    assert manager.running() == []


# --- the Hub login ----------------------------------------------------------


def _install_oauth_stubs(monkeypatch):
    """Put a fake callback server and a fake browser where hub_client looks them up."""
    made, opened = [], []

    class _Server:
        def __init__(self, address, handler):
            self.address, self.handler = address, handler
            self.stopped = threading.Event()
            made.append(self)

        def serve_forever(self):
            self.stopped.wait()

        def shutdown(self):
            self.stopped.set()

        def the_browser_returns(self, query):
            """Drive the handler login() defined, as the Hub's redirect would."""

            class _Request(self.handler):
                def __init__(self):  # no socket
                    pass

                def send_response(self, *a, **kw):
                    pass

                def send_header(self, *a, **kw):
                    pass

                def end_headers(self):
                    pass

            request = _Request()
            request.path = "/callback?" + query
            request.wfile = io.BytesIO()
            request.server = self
            request.do_GET()

    monkeypatch.setattr(hub_client_module, "HTTPServer", _Server)
    monkeypatch.setattr(
        hub_client_module, "open_url_outside_the_job", lambda url, **kw: opened.append(url)
    )
    return made, opened


@pytest.mark.asyncio
async def test_two_logins_at_once_run_one_oauth_flow(monkeypatch, caplog):
    made, opened = _install_oauth_stubs(monkeypatch)
    hub = HubClient("http://hub.invalid", oauth_callback_port=0)
    registered = []

    async def _register():
        registered.append(1)

    hub.register_node_id = _register

    first = asyncio.create_task(hub.login("google"))
    second = asyncio.create_task(hub.login("google"))
    try:
        with caplog.at_level(logging.INFO, logger=hub_client_module.logger.name):
            await _let_the_tasks_run()
            assert len(made) == 1, "a second callback server was started"
            assert len(opened) == 1, "a second browser tab was opened"

            made[0].the_browser_returns("access_token=T&refresh_token=R")
            await asyncio.wait_for(asyncio.gather(first, second), 5)

        assert len(made) == 1
        assert len(opened) == 1
        assert registered == [1]
        assert hub.jwt_token == "T"
        assert [r.getMessage() for r in caplog.records].count(
            "Already authenticated with google"
        ) == 1
    finally:
        for server in made:
            server.shutdown()
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        await hub.http_client.aclose()
