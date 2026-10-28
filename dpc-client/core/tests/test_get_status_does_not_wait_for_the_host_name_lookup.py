"""`get_status` must not hold the event loop while the host name is resolved.

On the Linux node a dead first DNS server made `socket.getaddrinfo(hostname)`
run 32-34 s, and `get_status` ran it on the loop: every UI connect froze the
whole backend for that long. The address scan now runs on its own worker, is
reused for `LOCAL_IPS_TTL_S`, and the host-name lookup in it is abandoned
after `HOSTNAME_LOOKUP_DEADLINE_S`.
"""

from __future__ import annotations

import asyncio
import socket
import time
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_module
from dpc_client_core.service import CoreService

SLOW_DNS_S = 1.0


class _Scanner:
    """The narrowest object the scan runs against: it needs the IPv4 filter only."""

    _is_usable_ipv4 = CoreService._is_usable_ipv4
    _scan_local_ips = CoreService._scan_local_ips
    _get_local_ips = CoreService._get_local_ips
    _local_ips_cache = None


def _status_stub(scanner) -> SimpleNamespace:
    return SimpleNamespace(
        hub_client=SimpleNamespace(websocket=None),
        p2p_manager=SimpleNamespace(
            peers={},
            node_id="dpc-node-0000000000000000",
            get_external_ips=lambda: [],
            peer_cache=SimpleNamespace(get_all_peers=lambda: []),
        ),
        peer_metadata={},
        llm_manager=SimpleNamespace(get_active_model_name=lambda: "m"),
        _get_local_ips=scanner._get_local_ips,
        settings=SimpleNamespace(get_p2p_listen_port=lambda: 8888),
        _external_ip=None,
        _is_global_ipv6=lambda ip: False,
        connection_status=SimpleNamespace(
            get_operation_mode=lambda: SimpleNamespace(value="online"),
            get_status_message=lambda: "ok",
            get_available_features=lambda: [],
        ),
        connection_orchestrator=None,
    )


@pytest.fixture
def slow_dns(monkeypatch):
    """getaddrinfo sleeps; the module's one-lookup-at-a-time flag starts clear."""
    calls: list[tuple] = []

    def slow_getaddrinfo(host, port, *args, **kwargs):
        calls.append((host, port))
        time.sleep(SLOW_DNS_S)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.7", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    monkeypatch.setattr(service_module, "_hostname_lookup_pending", False)
    monkeypatch.setattr(service_module.sys, "platform", "win32")  # no `ip addr`
    return calls


async def _loop_stalls_during(awaitable) -> tuple[float, object]:
    """The longest gap between two ticks of a 50 ms ticker while `awaitable` runs."""
    gaps: list[float] = []
    stop = asyncio.Event()

    async def ticker():
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    task = asyncio.create_task(ticker())
    try:
        result = await awaitable
    finally:
        stop.set()
        await task
    return max(gaps), result


@pytest.mark.asyncio
async def test_a_slow_host_name_lookup_does_not_stall_the_loop(slow_dns):
    scanner = _Scanner()

    worst_gap, status = await _loop_stalls_during(CoreService.get_status(_status_stub(scanner)))

    assert slow_dns, "the monkeypatched lookup was never reached, the test measures nothing"
    assert worst_gap < SLOW_DNS_S / 2, (
        f"the loop stood still for {worst_gap:.2f} s while the host name was resolved"
    )
    assert any(u["ip"] == "192.0.2.7" for u in status["dpc_uris"])


@pytest.mark.asyncio
async def test_the_second_status_within_the_ttl_does_not_look_again(slow_dns):
    scanner = _Scanner()
    stub = _status_stub(scanner)

    await CoreService.get_status(stub)
    looked_up = len(slow_dns)
    started = time.monotonic()
    again = await CoreService.get_status(stub)

    assert looked_up == 1
    assert len(slow_dns) == 1, "the second call within the TTL resolved the host name again"
    assert time.monotonic() - started < SLOW_DNS_S / 2
    assert any(u["ip"] == "192.0.2.7" for u in again["dpc_uris"])


def test_an_expired_entry_is_scanned_again(slow_dns, monkeypatch):
    monkeypatch.setattr(service_module, "LOCAL_IPS_TTL_S", 0.0)
    monkeypatch.setattr(service_module, "HOSTNAME_LOOKUP_DEADLINE_S", 5.0)
    scanner = _Scanner()

    scanner._get_local_ips()
    scanner._get_local_ips()

    assert len(slow_dns) == 2


def test_a_lookup_that_outlives_the_deadline_is_abandoned_and_not_repeated(monkeypatch):
    release = __import__("threading").Event()
    calls: list[str] = []

    def hung_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        release.wait(10)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", hung_getaddrinfo)
    monkeypatch.setattr(service_module, "_hostname_lookup_pending", False)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            service_module._bounded_hostname_lookup("h", 0.2)
        assert time.monotonic() - started < 2.0

        # the first is still pending: no second thread behind the same dead resolver
        with pytest.raises(TimeoutError, match="still pending"):
            service_module._bounded_hostname_lookup("h", 0.2)
        assert calls == ["h"]
    finally:
        release.set()


def test_the_scan_keeps_the_order_and_the_filters_of_the_two_lookups(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::5", 0, 0, 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 0, 0, 2)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.1.1", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.3.3", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.9", 0)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", ("192.0.2.9", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(service_module, "_hostname_lookup_pending", False)
    monkeypatch.setattr(service_module.sys, "platform", "win32")
    # the two UDP-connect methods find nothing on a box with no route
    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: (_ for _ in ()).throw(OSError("no route"))
    )

    scanner = _Scanner()
    found = scanner._scan_local_ips()

    assert found == ["192.0.2.9", "2001:db8::5"]


def test_an_empty_scan_is_not_kept(monkeypatch):
    scans: list[int] = []

    class Empty(_Scanner):
        def _scan_local_ips(self):
            scans.append(1)
            return []

    scanner = Empty()
    scanner._get_local_ips()
    scanner._get_local_ips()

    assert len(scans) == 2
