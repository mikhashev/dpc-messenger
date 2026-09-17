# dpc-client/core/dpc_client_core/own_addresses.py
"""Every address that is this machine, as far as it can tell.

An address is a guess: two machines behind one NAT share an external address, so
"this address is mine" can be wrong about someone else's node. Warn on it, never
refuse on it. A reply carrying our own node id is proof, and it settles the same
question one round trip later. See A-SEED-THAT-ANSWERED-IS-REPORTED-UNRESPONSIVE
and A-PEERS-CACHED-ENDPOINT on the board.
"""

import logging
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)

# Name "anywhere" or "here" rather than a host; comparing against them is always true.
_UNSPECIFIED = {"0.0.0.0", "::", ""}


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
