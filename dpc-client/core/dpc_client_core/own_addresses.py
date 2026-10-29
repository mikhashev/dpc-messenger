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
import sys
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Name "anywhere" or "here" rather than a host; comparing against them is always true.
_UNSPECIFIED = {"0.0.0.0", "::", ""}

# Interfaces that carry no local network of their own: tunnels, VPNs and virtual
# adapters. A full-tunnel VPN takes the default route, so "the address the OS
# would use toward 8.8.8.8" is the tunnel's, which no peer on the LAN can reach.
# Matched on the lower-cased interface name; psutil gives no adapter type. A
# name can be changed by the user (Windows lets an AmneziaVPN adapter be renamed
# "Ethernet 3"), so the name lists are backed by the link-layer test below.
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
    "anpi",       # macOS Apple network programming interface
)
# On macOS bridgeN is Internet Sharing or a VM network. On Linux bridge0 is often
# the host's own LAN: the NIC is enslaved to it and has no address of its own.
_DARWIN_ONLY_PREFIXES = (
    "bridge",     # macOS Internet Sharing / VM bridge
)
_LOOPBACK_NAMES = ("lo", "lo0")  # exact, since "lo" also starts "Local Area Connection"

# Private IPv4 ranges in the order a LAN address is preferred: home and office
# routers hand out 192.168/16 first, 10/8 is where VPNs and big sites live.
_PRIVATE_RANGES = tuple(
    ipaddress.ip_network(n) for n in ("192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8")
)


def _is_virtual_interface(name: str, platform: Optional[str] = None) -> bool:
    lowered = name.lower()
    if lowered in _LOOPBACK_NAMES:
        return True
    if (platform or sys.platform) == "darwin" and lowered.startswith(_DARWIN_ONLY_PREFIXES):
        return True
    return lowered.startswith(_VIRTUAL_NAME_PREFIXES) or any(
        token in lowered for token in _VIRTUAL_NAME_CONTAINS
    )


def _has_link_address(addrs, link_family) -> bool:
    """The interface reports a link-layer (MAC) address that is not empty or all zeros.

    A layer-3 tunnel has none: Wintun and WireGuard on Windows (psutil gives no
    AF_LINK entry at all, checked on the Windows node 2026-10-29), tun and wg on
    Linux, utun on macOS. Physical NICs, Hyper-V vEthernet, docker0 and bridges
    have one, so this tells a tunnel only from the rest; the name lists still
    drop the virtual adapters that do carry a MAC.
    """
    for addr in addrs:
        if addr.family != link_family:
            continue
        text = str(addr.address or "")
        if text.replace(":", "").replace("-", "").replace(".", "").strip("0"):
            return True
    return False


# The full-tunnel note is said once per process, not on every address scan.
_full_tunnel_noted = False


def _private_range_label(ip: str) -> Optional[str]:
    try:
        parsed = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    for net in _PRIVATE_RANGES:
        if parsed in net:
            return str(net)
    return None


def _note_full_tunnel(route_ip: Optional[str], candidates: List[str]) -> None:
    """One INFO line when the default route leaves through a private address on no LAN interface."""
    global _full_tunnel_noted
    if _full_tunnel_noted or not route_ip or route_ip in candidates:
        return
    label = _private_range_label(route_ip)
    if label is None:
        return
    _full_tunnel_noted = True
    logger.info(
        "The default route leaves through a private address in %s that is on no LAN "
        "interface: a full-tunnel VPN is likely; the LAN addresses are advertised instead",
        label,
    )


def lan_addresses(route_ip: Optional[str] = None) -> List[str]:
    """This node's IPv4 addresses on real local networks, 192.168/16 first.

    Interfaces that are up, not loopback, not tunnel/VPN/virtual (by name), with
    a link-layer address (a tunnel has none, whatever it is called), and with an
    address in a private range; 169.254/16 is not private and is dropped.
    Ordered 192.168/16, then 172.16/12, then 10/8. Inside a range the address
    of the default route comes first when `route_ip` (the source address toward
    the internet) is one of them, else the order is psutil's. Returns [] when
    the interfaces cannot be read: the caller falls back to the routing guess.

    A private `route_ip` that is none of them is a full-tunnel VPN holding the
    default route; that is logged once per process.
    """
    try:
        import socket as _socket

        import psutil

        stats = psutil.net_if_stats()
        all_addrs = psutil.net_if_addrs()
        link_family = getattr(psutil, "AF_LINK", None)
        # Trust "no MAC" only where the platform reports MACs at all.
        mac_known = link_family is not None and any(
            _has_link_address(addrs, link_family) for addrs in all_addrs.values()
        )
        found: List[tuple] = []
        for name, addrs in all_addrs.items():
            st = stats.get(name)
            if st is None or not st.isup or _is_virtual_interface(name):
                continue
            if mac_known and not _has_link_address(addrs, link_family):
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
        # sorted() is stable: inside a range the default route first, then psutil's order.
        for _, _, ip in sorted(
            ((rank, ip != route_ip, ip) for rank, ip in found), key=lambda t: t[:2]
        ):
            if ip not in ordered:
                ordered.append(ip)
        _note_full_tunnel(route_ip, ordered)
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
