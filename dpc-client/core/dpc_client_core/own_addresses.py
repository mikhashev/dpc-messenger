# dpc-client/core/dpc_client_core/own_addresses.py
"""Every address that is this machine, as far as it can tell.

An address is a guess: two machines behind one NAT share an external address, so
"this address is mine" can be wrong about someone else's node. Warn on it, never
refuse on it. A reply carrying our own node id is proof, and it settles the same
question one round trip later. See A-SEED-THAT-ANSWERED-IS-REPORTED-UNRESPONSIVE
and A-PEERS-CACHED-ENDPOINT on the board.
"""

import ipaddress
import logging
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Name "anywhere" or "here" rather than a host; comparing against them is always true.
_UNSPECIFIED = {"0.0.0.0", "::", ""}

# Interfaces that carry no local network of their own: tunnels, VPNs and virtual
# adapters. A full-tunnel VPN takes the default route, so "the address the OS
# would use toward 8.8.8.8" is the tunnel's, which no peer on the LAN can reach.
# Matched on the lower-cased interface name; psutil gives no adapter type.
_VIRTUAL_NAME_CONTAINS = (
    "amnezia",      # AmneziaVPN (WireGuard-based, Wintun adapter)
    "wireguard",    # WireGuard on Windows
    "wintun",       # Wintun adapter
    "openvpn",      # OpenVPN
    "tap-",         # OpenVPN TAP-Windows adapter
    "tap windows",  # TAP-Windows adapter, long name
    "tailscale",    # Tailscale
    "zerotier",     # ZeroTier
    "vethernet",    # Hyper-V / WSL virtual switch
    "hyper-v",      # Hyper-V adapters
    "virtualbox",   # VirtualBox host-only adapters
    "vmware",       # VMware adapters
    "docker",       # Docker Desktop adapters
    "bluetooth",    # Bluetooth PAN
    "loopback",     # Npcap / Microsoft loopback adapters
    "vpn",          # anything the user or a client named a VPN
)
_VIRTUAL_NAME_PREFIXES = (
    "wg",         # Linux WireGuard
    "tun",        # Linux/BSD tunnels
    "tap",        # Linux TAP devices
    "tailscale",  # Tailscale
    "zt",         # ZeroTier
    "ppp",        # point-to-point links
    "docker",     # Docker bridge
    "br-",        # Docker user-defined bridges
    "veth",       # container veth pairs
    "virbr",      # libvirt bridge
    "vmnet",      # VMware (Linux and macOS)
    "vboxnet",    # VirtualBox host-only
    "utun",       # macOS tunnels (VPNs, iCloud Private Relay)
    "ipsec",      # macOS IPsec
    "awdl",       # macOS AirDrop link
    "llw",        # macOS low-latency WLAN
    "bridge",     # macOS Internet Sharing / VM bridge
    "anpi",       # macOS Apple network programming interface
)
_LOOPBACK_NAMES = ("lo", "lo0")  # exact, since "lo" also starts "Local Area Connection"

# Private IPv4 ranges in the order a LAN address is preferred: home and office
# routers hand out 192.168/16 first, 10/8 is where VPNs and big sites live.
_PRIVATE_RANGES = tuple(
    ipaddress.ip_network(n) for n in ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8")
)


def _is_virtual_interface(name: str) -> bool:
    lowered = name.lower()
    if lowered in _LOOPBACK_NAMES:
        return True
    return lowered.startswith(_VIRTUAL_NAME_PREFIXES) or any(
        token in lowered for token in _VIRTUAL_NAME_CONTAINS
    )


def lan_addresses() -> List[str]:
    """This node's IPv4 addresses on real local networks, 192.168/16 first.

    Interfaces that are up, not loopback, not tunnel/VPN/virtual (by name), with
    an address in a private range; 169.254/16 is not private and is dropped.
    Ordered 192.168/16, then 172.16/12, then 10/8, stable inside each. Returns []
    when the interfaces cannot be read: the caller falls back to the routing guess.
    """
    try:
        import socket as _socket

        import psutil

        stats = psutil.net_if_stats()
        found: List[tuple] = []
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is None or not st.isup or _is_virtual_interface(name):
                continue
            for addr in addrs:
                if addr.family != _socket.AF_INET:
                    continue
                try:
                    ip = ipaddress.IPv4Address(str(addr.address).split("%")[0])
                except ValueError:
                    continue
                for rank, net in enumerate(_PRIVATE_RANGES):
                    if ip in net:
                        found.append((rank, str(ip)))
                        break
        ordered: List[str] = []
        for _, ip in sorted(found, key=lambda t: t[0]):  # sorted() is stable
            if ip not in ordered:
                ordered.append(ip)
        return ordered
    except Exception as e:  # noqa: BLE001 — address discovery must not stop a start-up
        logger.warning("Could not list LAN addresses: %s", e)
        return []


class OwnAddresses:
    """The set of addresses this node has learned belong to it."""

    def __init__(self) -> None:
        self._addresses: Set[str] = set()

    def learn(self, ip: Optional[str], source: str = "") -> None:
        if not ip or ip in _UNSPECIFIED:
            return
        if ip not in self._addresses:
            self._addresses.add(ip)
            logger.debug("Own address learned: %s%s", ip, f" ({source})" if source else "")

    def learn_all(self, ips: Iterable[str], source: str = "") -> None:
        for ip in ips:
            self.learn(ip, source)

    def learn_local_interfaces(self) -> Set[str]:
        """Every address on this host's interfaces, loopback included."""
        found: Set[str] = set()
        try:
            import socket as _socket

            import psutil

            families = {_socket.AF_INET, getattr(_socket, "AF_INET6", _socket.AF_INET)}
            for addrs in psutil.net_if_addrs().values():
                for addr in addrs:
                    if addr.family in families:
                        # A %scope suffix cannot match a configured seed.
                        found.add(str(addr.address).split("%")[0])
        except Exception as e:  # noqa: BLE001 — address discovery must not stop a start-up
            logger.warning("Could not enumerate local interfaces: %s", e)

        self.learn_all(found, "local interface")
        return found

    def __contains__(self, ip: object) -> bool:
        return isinstance(ip, str) and ip in self._addresses

    def contains(self, ip: Optional[str]) -> bool:
        return bool(ip) and ip in self._addresses

    def known(self) -> Set[str]:
        return set(self._addresses)

    def self_seeds(self, seeds: Iterable) -> list:
        """The (ip, port) seeds whose address is one of ours."""
        return [(ip, port) for ip, port in seeds if self.contains(ip)]
