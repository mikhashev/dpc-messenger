"""After the five fast attempts, a peer we keep is retried slowly until it answers.

Observed on the Linux node 2026-09-21: link lost 16:39:55, five attempts by
16:43:28, «Gave up reconnecting», then nothing for 24 hours while the service
ran; the same address answered in 3 s when someone clicked Connect 47 h later
(RECONNECT-GIVES-UP-AFTER-FIVE-ATTEMPTS-AND-NEVER-TRIES-AGAIN-UNTIL-THE-SERVICE-RESTARTS).

No network here: the orchestrator is a fake that fails until told the peer is
back, and asyncio.sleep is replaced by one that records the delay and yields.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dpc_client_core.service import CoreService

US = "dpc-node-" + "a" * 32
PEER = "dpc-node-" + "b" * 32
REAL_SLEEP = asyncio.sleep


class FakeOrchestrator:
    def __init__(self, service, fail_first):
        self.service = service
        self.fail_first = fail_first
        self.calls = 0

    async def connect(self, node_id):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ConnectionError("All strategies exhausted")
        self.service.p2p_manager.peers[node_id] = object()
        return SimpleNamespace(strategy_used="ipv4_direct")


def _service(fail_first=10**6, auto_connect=True, ceiling=900.0):
    s = object.__new__(CoreService)
    s._is_running = True
    s._background_tasks = set()
    s._reconnect_tasks = {}
    s._reconnect_slow_phase = set()
    s._user_disconnected_peers = set()
    s.p2p_manager = SimpleNamespace(node_id=US, peers={}, peer_cache=None)
    s.firewall = SimpleNamespace(node_groups={"friends": [PEER]})
    s.group_manager = None
    s.settings = SimpleNamespace(
        get_p2p_auto_connect_node_groups=lambda: auto_connect,
        get_reconnect_slow_interval_max_seconds=lambda: ceiling,
    )
    s.local_api = SimpleNamespace(broadcast_event=AsyncMock())
    s.file_transfer_manager = SimpleNamespace(active_transfers={})
    s._history_requested_peers = set()
    s.history_requests = SimpleNamespace(forget_peer=lambda peer_id: None)
    s.connection_orchestrator = FakeOrchestrator(s, fail_first)
    s.p2p_coordinator = SimpleNamespace(disconnect=AsyncMock())
    return s


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []

    async def fake_sleep(delay, *args, **kwargs):
        recorded.append(delay)
        await REAL_SLEEP(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return recorded


def _reconnect_tasks(service):
    return [t for t in service._background_tasks
            if t.get_name().startswith("reconnect_") and not t.done()]


async def _spin(n=200):
    for _ in range(n):
        await REAL_SLEEP(0)


async def _until(predicate, n=5000):
    for _ in range(n):
        if predicate():
            return True
        await REAL_SLEEP(0)
    return predicate()


def test_the_slow_retry_reaches_a_peer_that_comes_back_after_the_fast_attempts(sleeps):
    async def run():
        service = _service(fail_first=9)  # five fast misses, four slow ones
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service.p2p_manager.peers)
        return service

    service = asyncio.run(run())
    assert service.connection_orchestrator.calls == 10
    slow = sleeps[5:]
    # 60 doubling, each within the jitter band, never far past the ceiling
    for base, got in zip([60, 120, 240, 480, 900], slow):
        assert base * 0.8 <= got <= base * 1.2
    assert service._reconnect_tasks == {}


def test_the_backoff_stops_growing_at_the_ceiling(sleeps):
    async def run():
        service = _service(fail_first=12, ceiling=300.0)
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service.p2p_manager.peers)

    asyncio.run(run())
    assert max(sleeps[5:]) <= 300 * 1.2
    assert min(sleeps[-3:]) >= 300 * 0.8


def test_a_second_disconnect_does_not_start_a_second_loop(sleeps):
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        await service._handle_peer_disconnected(PEER)
        await _spin(5)
        count = len(_reconnect_tasks(service))
        service._is_running = False
        for t in list(service._background_tasks):
            t.cancel()
        await asyncio.gather(*service._background_tasks, return_exceptions=True)
        return count

    assert asyncio.run(run()) == 1


def test_a_disconnect_during_the_slow_phase_starts_over_fast(sleeps):
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service._reconnect_slow_phase)
        first = service._reconnect_tasks[PEER]
        await service._handle_peer_disconnected(PEER)
        second = service._reconnect_tasks[PEER]
        await _spin(5)
        alive = len(_reconnect_tasks(service))
        slow = PEER in service._reconnect_slow_phase
        service._is_running = False
        for t in list(service._background_tasks):
            t.cancel()
        await asyncio.gather(*service._background_tasks, return_exceptions=True)
        return first, second, alive, slow

    first, second, alive, slow = asyncio.run(run())
    assert first is not second and first.cancelled()
    assert alive == 1 and not slow


def test_the_users_disconnect_stops_the_slow_loop_and_schedules_nothing(sleeps):
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service._reconnect_slow_phase)
        task = service._reconnect_tasks[PEER]
        await service.disconnect_from_peer(PEER)
        # the disconnect callback fires again for the user's own Disconnect
        await service._handle_peer_disconnected(PEER)
        await _spin()
        calls = service.connection_orchestrator.calls
        await _spin()
        return service, task, calls

    service, task, calls = asyncio.run(run())
    assert task.done()
    assert _reconnect_tasks(service) == []
    assert service.connection_orchestrator.calls == calls


def test_a_user_disconnect_is_not_redialled_by_the_fast_attempts(sleeps):
    async def run():
        service = _service()
        await service.disconnect_from_peer(PEER)
        await service._handle_peer_disconnected(PEER)
        await _spin()
        return service

    service = asyncio.run(run())
    assert service.connection_orchestrator.calls == 0


def test_a_new_link_clears_the_users_disconnect(sleeps):
    async def run():
        service = _service()
        service._user_disconnected_peers.add(PEER)
        service.p2p_manager.peers[PEER] = object()
        service._settle_reconnects_for_connected_peers()
        return service

    assert PEER not in asyncio.run(run())._user_disconnected_peers


def test_leaving_every_shared_group_stops_the_slow_loop(sleeps):
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service._reconnect_slow_phase)
        task = service._reconnect_tasks[PEER]
        service.firewall.node_groups = {}
        assert await _until(task.done)
        calls = service.connection_orchestrator.calls
        await _spin()
        return service, task, calls

    service, task, calls = asyncio.run(run())
    assert task.done() and not task.cancelled()
    assert service.connection_orchestrator.calls == calls
    assert service._reconnect_tasks == {}


def test_a_peer_not_worth_dialling_is_not_retried_slowly(sleeps):
    """_worth_dialling is asked on every wake, not only when scheduling."""
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service._reconnect_slow_phase)
        task = service._reconnect_tasks[PEER]
        inbound = SimpleNamespace(last_connection_direction="in")
        service.p2p_manager.peer_cache = SimpleNamespace(get_peer=lambda nid: inbound)
        done = await _until(task.done)
        task.cancel()
        return done, task

    done, task = asyncio.run(run())
    assert done and not task.cancelled()


def test_the_auto_connect_switch_keeps_the_slow_phase_off(sleeps):
    async def run():
        service = _service(auto_connect=False)
        await service._handle_peer_disconnected(PEER)
        await _until(lambda: not _reconnect_tasks(service))
        return service

    service = asyncio.run(run())
    assert service.connection_orchestrator.calls == 5
    assert PEER not in service._reconnect_slow_phase


def test_shutdown_cancels_the_slow_loop(sleeps):
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service._reconnect_slow_phase)
        task = service._reconnect_tasks[PEER]
        # what CoreService.shutdown does with its background tasks
        service._is_running = False
        for t in list(service._background_tasks):
            t.cancel()
        await asyncio.gather(*service._background_tasks, return_exceptions=True)
        return service, task

    service, task = asyncio.run(run())
    assert task.cancelled()
    assert service._reconnect_tasks == {}
    assert service._reconnect_slow_phase == set()


def test_a_link_made_elsewhere_ends_a_sleeping_slow_loop(sleeps):
    async def run():
        service = _service()
        await service._handle_peer_disconnected(PEER)
        assert await _until(lambda: PEER in service._reconnect_slow_phase)
        task = service._reconnect_tasks[PEER]
        service.p2p_manager.peers[PEER] = object()  # manual connect or inbound
        service._settle_reconnects_for_connected_peers()
        await _spin(5)
        return service, task

    service, task = asyncio.run(run())
    assert task.cancelled()
    assert service._reconnect_tasks == {}
