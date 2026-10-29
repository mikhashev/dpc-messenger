"""Behind a full-tunnel VPN the default route is the tunnel; the LAN address is the other one.

Measured on the Windows node: AmneziaVPN (WireGuard-based, a Wintun adapter) carries
the default route, so "the source address toward 8.8.8.8" was the tunnel's 10.x
address and the DHT announce and the UI's first "Local Network" entry both named an
address no peer on the Wi-Fi can reach. `lan_addresses` lists the addresses of real
local networks by interface name; the announce prefers its first, the old routing
guess stays as the fallback, and the tunnel address is still learned as our own.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from dpc_client_core import service as service_module
from dpc_client_core.own_addresses import OwnAddresses, lan_addresses
from dpc_client_core.p2p_manager import P2PManager
from dpc_client_core.service import CoreService

AF = socket.AF_INET


def _addr(ip: str):
    return SimpleNamespace(family=AF, address=ip)


def _fake_psutil(monkeypatch, interfaces: dict, up: bool = True, down=()):
    """interfaces: name -> [ip]; every interface is up except those in `down`."""
    fake = SimpleNamespace(
        net_if_addrs=lambda: {n: [_addr(i) for i in ips] for n, ips in interfaces.items()},
        net_if_stats=lambda: {
            n: SimpleNamespace(isup=(n not in down)) for n in interfaces
        },
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)


# --------------------------------------------------------------------------
# The helper, per platform
# --------------------------------------------------------------------------

def test_windows_wifi_is_kept_and_vpn_hyperv_and_loopback_are_dropped(monkeypatch):
    _fake_psutil(monkeypatch, {
        "Wi-Fi": ["192.168.0.5"],
        "AmneziaVPN": ["10.8.1.5"],
        "vEthernet (WSL)": ["172.20.0.1"],
        "Loopback Pseudo-Interface 1": ["127.0.0.1"],
    })

    assert lan_addresses() == ["192.168.0.5"]


def test_linux_eth0_is_kept_and_wireguard_and_docker_are_dropped(monkeypatch):
    _fake_psutil(monkeypatch, {
        "lo": ["127.0.0.1"],
        "eth0": ["192.168.1.20"],
        "wg0": ["10.0.0.2"],
        "docker0": ["172.17.0.1"],
    })

    assert lan_addresses() == ["192.168.1.20"]


def test_macos_en0_is_kept_and_utun_and_bridge_are_dropped(monkeypatch):
    _fake_psutil(monkeypatch, {
        "lo0": ["127.0.0.1"],
        "en0": ["192.168.1.30"],
        "utun3": ["10.8.0.2"],
        "bridge100": ["192.168.64.1"],
    })

    assert lan_addresses() == ["192.168.1.30"]


def test_a_corporate_lan_in_10_8_on_a_physical_nic_is_kept(monkeypatch):
    _fake_psutil(monkeypatch, {"Ethernet": ["10.20.30.40"], "Tailscale": ["100.64.0.9"]})

    assert lan_addresses() == ["10.20.30.40"]


def test_the_result_runs_192_168_then_172_16_then_10(monkeypatch):
    _fake_psutil(monkeypatch, {
        "eth0": ["10.1.1.1"],
        "eth1": ["172.16.5.5"],
        "eth2": ["192.168.7.7"],
        "eth3": ["192.168.8.8"],
    })

    assert lan_addresses() == ["192.168.7.7", "192.168.8.8", "172.16.5.5", "10.1.1.1"]


def test_a_down_interface_a_link_local_and_a_public_address_are_dropped(monkeypatch):
    _fake_psutil(
        monkeypatch,
        {
            "eth0": ["192.168.1.2"],
            "eth1": ["169.254.3.4"],
            "eth2": ["198.51.100.8"],
            "eth3": ["192.168.9.9"],
        },
        down=("eth3",),
    )

    assert lan_addresses() == ["192.168.1.2"]


def test_a_local_area_connection_is_not_mistaken_for_loopback(monkeypatch):
    """Exact names for lo/lo0: "Local Area Connection" starts with "lo"."""
    _fake_psutil(monkeypatch, {"Local Area Connection": ["192.168.2.2"]})

    assert lan_addresses() == ["192.168.2.2"]


def test_psutil_raising_gives_an_empty_list(monkeypatch):
    def _boom():
        raise OSError("no interfaces here")

    fake = SimpleNamespace(net_if_addrs=_boom, net_if_stats=_boom)
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake)

    assert lan_addresses() == []


# --------------------------------------------------------------------------
# The DHT announce address
# --------------------------------------------------------------------------

TUNNEL = "10.8.1.5"
LAN = "192.168.0.5"


def _manager(route_ip: str):
    m = P2PManager.__new__(P2PManager)
    m.own_addresses = OwnAddresses()
    m._default_route_source_ip = lambda: route_ip
    return m


@pytest.mark.asyncio
async def test_the_announce_ip_is_the_lan_address_when_there_is_one(monkeypatch):
    import dpc_client_core.p2p_manager as p2p_module

    monkeypatch.setattr(p2p_module, "lan_addresses", lambda: [LAN])
    m = _manager(TUNNEL)

    assert await m._get_primary_local_ip() == LAN


@pytest.mark.asyncio
async def test_the_announce_ip_falls_back_to_the_route_guess_with_no_lan(monkeypatch):
    import dpc_client_core.p2p_manager as p2p_module

    monkeypatch.setattr(p2p_module, "lan_addresses", lambda: [])
    m = _manager(TUNNEL)

    assert await m._get_primary_local_ip() == TUNNEL


@pytest.mark.asyncio
async def test_the_tunnel_address_is_still_learned_as_our_own(monkeypatch):
    import dpc_client_core.p2p_manager as p2p_module

    monkeypatch.setattr(p2p_module, "lan_addresses", lambda: [LAN])
    m = _manager(TUNNEL)

    await m._get_primary_local_ip()

    assert m.own_addresses.contains(TUNNEL)
    assert m.own_addresses.contains(LAN)


# --------------------------------------------------------------------------
# The UI list
# --------------------------------------------------------------------------

class _Scanner:
    _is_usable_ipv4 = CoreService._is_usable_ipv4
    _scan_local_ips = CoreService._scan_local_ips
    _get_local_ips = CoreService._get_local_ips
    _local_ips_cache = None


class _RouteSocket:
    """UDP "connect" that reports the tunnel as the source toward the internet."""

    def __init__(self, family, *a, **k):
        self.family = family

    def connect(self, addr):
        if self.family != socket.AF_INET:
            raise OSError("no IPv6 here")

    def getsockname(self):
        return (TUNNEL, 0)

    def close(self):
        pass


def test_the_ui_list_starts_with_the_lan_address_and_keeps_the_tunnel(monkeypatch):
    monkeypatch.setattr(service_module, "lan_addresses", lambda: [LAN])
    monkeypatch.setattr(service_module.socket, "socket", _RouteSocket)
    monkeypatch.setattr(service_module, "_bounded_hostname_lookup", lambda *a, **k: [])

    ips = _Scanner()._scan_local_ips()

    assert ips[0] == LAN
    assert TUNNEL in ips
    assert ips.count(LAN) == 1


def test_the_ui_list_is_unchanged_when_there_is_no_lan_address(monkeypatch):
    monkeypatch.setattr(service_module, "lan_addresses", lambda: [])
    monkeypatch.setattr(service_module.socket, "socket", _RouteSocket)
    monkeypatch.setattr(service_module, "_bounded_hostname_lookup", lambda *a, **k: [])

    assert _Scanner()._scan_local_ips()[0] == TUNNEL
