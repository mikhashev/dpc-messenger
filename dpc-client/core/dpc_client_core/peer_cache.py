# dpc-client/core/dpc_client_core/peer_cache.py

import asyncio
import ipaddress
import json
import logging
import re
from pathlib import Path
from typing import Optional, List, Dict, Any, Awaitable, Callable, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# The port a node listens on when nobody has said otherwise — settings.py's
# [p2p] listen_port default. It is what a peer we have never dialled is assumed
# to be on, never what we overwrite a known endpoint with.
DEFAULT_DIRECT_PORT = 8888

# A ninth address evicts the one with the oldest last_seen.
MAX_ADDRESSES = 8

# Budget for one cached address when more follow it: a dead route costs the
# next one at most this. The last address gets whatever budget is left.
CACHED_ADDRESS_DIAL_TIMEOUT_S = 10.0

# RFC 1918, IPv6 ULA and loopback, spelled out: ipaddress's is_private also
# counts the RFC 5737 documentation ranges.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
              "fc00::/7", "::1/128")
)

# `local_addresses` (DPTP 1.12 §3.1): a peer's own LAN "ip:port" list, a hint
# where to dial. Kept in metadata, replaced per HELLO/HELLO_ACK, and never put
# into `addresses` until a dial over it succeeds.
LOCAL_ADDRESSES_KEY = "local_addresses"
MAX_LOCAL_ADDRESSES = 4
_RFC1918 = tuple(
    ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_PORT_TEXT = re.compile(r"[0-9]{1,5}")

# A LAN host answers a connect in milliseconds or refuses at once; 2 s also
# covers the TLS handshake, and caps a candidate on another network at 2 s.
ADVERTISED_ADDRESS_DIAL_TIMEOUT_S = 2.0


def checked_local_addresses(value: Any, own_addresses: Any = None) -> Optional[List[str]]:
    """The usable entries of a received `local_addresses`; None when it is not a list.

    The first MAX_LOCAL_ADDRESSES entries, each kept if it is "ip:port" with a
    private RFC 1918 IPv4 address, a port of 1-65535, not one of our own
    addresses and not repeated. Drops are logged at DEBUG by count, not address.
    """
    if not isinstance(value, list):
        if value is not None:
            logger.debug("local_addresses is not a list (%s), ignored", type(value).__name__)
        return None
    kept: List[str] = []
    dropped: Dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    if len(value) > MAX_LOCAL_ADDRESSES:
        dropped["over the cap"] = len(value) - MAX_LOCAL_ADDRESSES
    for item in value[:MAX_LOCAL_ADDRESSES]:
        if not isinstance(item, str) or item.count(":") != 1:
            drop("not ip:port")
            continue
        host, port_text = item.split(":")
        try:
            ip = ipaddress.IPv4Address(host)
        except ValueError:
            drop("not IPv4")
            continue
        if not any(ip in net for net in _RFC1918):
            drop("not RFC 1918")
            continue
        if not _PORT_TEXT.fullmatch(port_text) or not 1 <= int(port_text) <= 65535:
            drop("bad port")
            continue
        if own_addresses is not None and own_addresses.contains(str(ip)):
            drop("our own address")
            continue
        entry = f"{ip}:{int(port_text)}"
        if entry in kept:
            drop("repeated")
            continue
        kept.append(entry)
    if dropped:
        logger.debug("local_addresses: kept %d, dropped %s", len(kept), dropped)
    return kept


def local_addresses_to_advertise(lan: List[str], port: Optional[int]) -> List[str]:
    """This node's `local_addresses`: its LAN addresses with the P2P listen port, at most four."""
    if not port:
        return []
    out: List[str] = []
    for ip in lan:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if any(addr in net for net in _RFC1918):
            out.append(f"{addr}:{port}")
        if len(out) == MAX_LOCAL_ADDRESSES:
            break
    return out


def normalise_ip(ip: str) -> str:
    """One spelling per address: no brackets, no %zone, IPv4-mapped IPv6 as IPv4.

    Brackets belong to the "[host]:port" form of a dpc:// URI, not to the stored
    host; the zone is dropped as own_addresses and the DHT drop it. A host name
    is kept, lower-cased.
    """
    host = (ip or "").strip()
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    host = host.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host.lower()
    if addr.version == 6 and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def _seen_at(entry: Dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(entry.get("last_seen")).timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def addresses_to_try(cached: Any, own_addresses: Any = None) -> List[Dict[str, Any]]:
    """The cached addresses of a peer in the order to dial them.

    First the peer's advertised `local_addresses` (new entries with the short
    `dial_timeout_s`), minus any already answered or our own; then the
    answered `addresses`: local-network addresses first, as ICE puts host candidates first (see
    ipv4_direct.py), then the rest by most recent last_seen, then IPv4
    link-local. IPv6 link-local is left out: without its zone it cannot be
    dialled. An address that is also this node's own goes last, not nowhere:
    behind a shared exit it reaches the peer too (connection_orchestrator.py).
    """
    entries = list(getattr(cached, "addresses", None) or [])
    if not entries and getattr(cached, "last_direct_ip", None):
        entries = [{
            "ip": normalise_ip(cached.last_direct_ip),
            "port": getattr(cached, "last_direct_port", None) or DEFAULT_DIRECT_PORT,
            "last_seen": getattr(cached, "last_seen", None),
            "direction": getattr(cached, "last_connection_direction", None),
        }]

    def rank(entry: Dict[str, Any]) -> Optional[int]:
        try:
            addr = ipaddress.ip_address(entry["ip"])
        except (KeyError, TypeError, ValueError):
            return 1
        if any(addr in net for net in _LOCAL_NETWORKS):
            return 0
        if addr.is_link_local:
            return None if addr.version == 6 else 2
        return 1

    ordered = []
    for entry in entries:
        r = rank(entry)
        if r is None:
            continue
        own = bool(own_addresses is not None and own_addresses.contains(entry.get("ip")))
        ordered.append(((own, r, -_seen_at(entry)), entry))
    ordered.sort(key=lambda pair: pair[0])
    answered = [entry for _, entry in ordered]
    return _advertised_candidates(cached, answered, own_addresses) + answered


def _advertised_candidates(cached: Any, answered: List[Dict[str, Any]], own_addresses: Any) -> List[Dict[str, Any]]:
    """The peer's `local_addresses` not yet answered, as dial entries with the short budget."""
    metadata = getattr(cached, "metadata", None) or {}
    stored = checked_local_addresses(metadata.get(LOCAL_ADDRESSES_KEY), own_addresses) or []
    have = {(e.get("ip"), e.get("port")) for e in answered}
    out: List[Dict[str, Any]] = []
    for text in stored:
        ip, port_text = text.split(":")
        if (ip, int(port_text)) in have:
            continue
        out.append({"ip": ip, "port": int(port_text),
                    "dial_timeout_s": ADVERTISED_ADDRESS_DIAL_TIMEOUT_S})
    return out


async def dial_in_order(
    dial: Callable[[str, int, float], Awaitable[Any]],
    entries: List[Dict[str, Any]],
    total_timeout: float,
    per_address_timeout: float = CACHED_ADDRESS_DIAL_TIMEOUT_S,
) -> Tuple[Any, Optional[Dict[str, Any]], Optional[BaseException]]:
    """Dial each entry in turn and stop on the first that answers.

    An entry with `dial_timeout_s` gets at most that, wherever it stands. Every
    other address but the last gets at most per_address_timeout, the last what
    is left of total_timeout. Returns (connection, entry, None) on success,
    (None, None, last_error) otherwise.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout
    last_error: Optional[BaseException] = None
    for i, entry in enumerate(entries):
        left = deadline - loop.time()
        if left <= 0:
            break
        cap = entry.get("dial_timeout_s")
        if cap:
            budget = min(cap, left)
        else:
            budget = left if i == len(entries) - 1 else min(per_address_timeout, left)
        try:
            connection = await asyncio.wait_for(
                dial(entry["ip"], entry["port"], budget), timeout=budget
            )
            return connection, entry, None
        except Exception as e:  # noqa: BLE001 — the next address may work
            logger.debug("Cached address %s:%s failed: %s", entry.get("ip"), entry.get("port"), e)
            last_error = e
    return None, None, last_error


@dataclass
class CachedPeer:
    """Information about a known peer."""
    node_id: str
    display_name: Optional[str] = None
    last_seen: Optional[str] = None  # ISO format timestamp
    last_direct_ip: Optional[str] = None  # For Direct TLS fallback
    last_direct_port: int = DEFAULT_DIRECT_PORT
    supports_webrtc: bool = False
    supports_direct: bool = False
    # Who placed the last connection that worked: "out" if we dialled the peer,
    # "in" if it dialled us. Absent means neither has been recorded yet. Read by
    # the reconnect decision — a peer that reaches us but cannot be reached will
    # come back on its own, and dialling it is a guaranteed miss.
    last_connection_direction: Optional[str] = None
    metadata: Dict[str, Any] = None  # Additional peer metadata
    # Every address a direct connection worked on, one entry per ip:
    # {"ip", "port", "last_seen", "direction"}, the last two meaning what they
    # mean above, per address. last_direct_ip/_port mirror the newest entry.
    addresses: List[Dict[str, Any]] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}
        if self.addresses is None:
            # A row written before the list existed: its one address is the list.
            self.addresses = []
            if self.last_direct_ip:
                self.addresses.append({
                    "ip": normalise_ip(self.last_direct_ip),
                    "port": self.last_direct_port,
                    "last_seen": self.last_seen,
                    "direction": self.last_connection_direction,
                })

    def record_address(self, ip: str, port: Optional[int], direction: Optional[str]) -> None:
        """Add the address a connection worked on, or refresh its entry.

        Never replaces another entry. A port of None keeps the entry's port (an
        inbound connection's source port is not the peer's listener); a new entry
        with no port starts at DEFAULT_DIRECT_PORT. A direction of None keeps the
        recorded one.
        """
        ip = normalise_ip(ip)
        now = datetime.now(timezone.utc).isoformat()
        entry = next((e for e in self.addresses if e.get("ip") == ip), None)
        if entry is None:
            entry = {
                "ip": ip,
                "port": port if port is not None else DEFAULT_DIRECT_PORT,
                "last_seen": now,
                "direction": direction,
            }
            self.addresses.append(entry)
        else:
            if port is not None:
                entry["port"] = port
            entry["last_seen"] = now
            if direction:
                entry["direction"] = direction
        while len(self.addresses) > MAX_ADDRESSES:
            self.addresses.remove(min(self.addresses, key=_seen_at))
        self.last_direct_ip = entry["ip"]
        self.last_direct_port = entry["port"]

    def is_recently_seen(self, hours: int = 24) -> bool:
        """Check if peer was seen recently."""
        if not self.last_seen:
            return False
        try:
            last_seen_dt = datetime.fromisoformat(self.last_seen)
            threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
            return last_seen_dt >= threshold
        except Exception:
            return False


class PeerCache:
    """
    Manages cached information about known peers for offline operation.

    Stores peer metadata to enable:
    - Direct TLS connection attempts without Hub
    - Display of known peers when Hub is offline
    - Connection history and preferences
    """

    def __init__(self, cache_file: Path):
        """
        Initialize peer cache.

        Args:
            cache_file: Path to peer cache JSON file
        """
        self.cache_file = cache_file
        self._peers: Dict[str, CachedPeer] = {}
        self._load_cache()

    def _load_cache(self):
        """Load peer cache from disk."""
        if not self.cache_file.exists():
            logger.debug("Peer cache not found at %s, starting fresh", self.cache_file)
            return

        try:
            with open(self.cache_file, 'r') as f:
                data = json.load(f)

            peers_data = data.get("peers", [])
            self._peers = {}

            for peer_dict in peers_data:
                peer_dict = dict(peer_dict)
                metadata = dict(peer_dict.get("metadata") or {})
                if "addresses" in metadata:
                    peer_dict.setdefault("addresses", metadata.pop("addresses"))
                    peer_dict["metadata"] = metadata
                peer = CachedPeer(**peer_dict)
                self._peers[peer.node_id] = peer

            logger.info("Loaded %d peers from cache", len(self._peers))

        except Exception as e:
            logger.error("Error loading peer cache: %s", e, exc_info=True)
            self._peers = {}

    def _save_cache(self):
        """Save peer cache to disk."""
        try:
            # Ensure directory exists
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            # addresses is written inside metadata: an older build's
            # CachedPeer(**row) refuses an unknown key, which drops every peer,
            # and carries metadata through untouched.
            peers_data = []
            for peer in self._peers.values():
                row = asdict(peer)
                row["metadata"] = {**row["metadata"], "addresses": row.pop("addresses")}
                peers_data.append(row)

            data = {
                "version": "1.0",
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "peers": peers_data
            }

            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)

            logger.debug("Saved %d peers to cache", len(self._peers))

        except Exception as e:
            logger.error("Error saving peer cache: %s", e, exc_info=True)

    def add_or_update_peer(
        self,
        node_id: str,
        display_name: Optional[str] = None,
        direct_ip: Optional[str] = None,
        direct_port: Optional[int] = None,
        supports_webrtc: bool = False,
        supports_direct: bool = False,
        direction: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Add or update peer information.

        Args:
            node_id: Peer's node ID
            display_name: Peer's display name
            direct_ip: An IP a direct connection worked on: added to the
                peer's addresses or its entry refreshed, never replacing others.
            direct_port: Direct TLS port. None means "I do not know it" and
                leaves whatever is recorded alone; it used to default to 8888,
                which made a caller that could not know indistinguishable from
                one asserting the default. CoreService.on_peer_list_change is
                that caller — it reads the ip off the live socket and has no
                way to learn the peer's listening port — so every dial to a
                peer off 8888 was cached correctly and then flattened to 8888
                milliseconds later, and the next reconnect dialled the wrong
                door. A new peer with no port still starts at
                DEFAULT_DIRECT_PORT.
            supports_webrtc: Whether peer supports WebRTC
            supports_direct: Whether peer supports Direct TLS
            direction: "out" when this node dialled the peer, "in" when the peer
                dialled us; None leaves whatever was recorded before, so a
                caller that does not know does not erase what a caller that did
                know wrote.
            metadata: Additional metadata
        """
        if node_id in self._peers:
            # Update existing peer
            peer = self._peers[node_id]
            if display_name:
                peer.display_name = display_name
            if direct_ip:
                peer.record_address(direct_ip, direct_port, direction)
            peer.supports_webrtc = supports_webrtc
            peer.supports_direct = supports_direct
            if direction:
                peer.last_connection_direction = direction
            peer.last_seen = datetime.now(timezone.utc).isoformat()
            if metadata:
                peer.metadata.update(metadata)
        else:
            # Create new peer
            peer = CachedPeer(
                node_id=node_id,
                display_name=display_name,
                last_seen=datetime.now(timezone.utc).isoformat(),
                last_direct_port=(
                    direct_port if direct_port is not None else DEFAULT_DIRECT_PORT
                ),
                supports_webrtc=supports_webrtc,
                supports_direct=supports_direct,
                last_connection_direction=direction,
                metadata=metadata or {}
            )
            if direct_ip:
                peer.record_address(direct_ip, direct_port, direction)
            self._peers[node_id] = peer

        self._save_cache()

    def set_local_addresses(self, node_id: str, local_addresses: Optional[List[str]]) -> None:
        """Replace a cached peer's `local_addresses`; None (field absent) changes nothing.

        Replaced, never merged; an empty list clears it. `addresses` is not touched.
        """
        peer = self._peers.get(node_id)
        if local_addresses is None or peer is None:
            return
        if not local_addresses and LOCAL_ADDRESSES_KEY not in peer.metadata:
            return
        if peer.metadata.get(LOCAL_ADDRESSES_KEY) == local_addresses:
            return
        peer.metadata[LOCAL_ADDRESSES_KEY] = list(local_addresses)
        self._save_cache()

    def remove_address(self, node_id: str, ip: str, port: Optional[int]) -> bool:
        """Forget one address of a cached peer; True when something was removed.

        For an address a dial found to lead somewhere else (back to this node).
        It leaves `addresses` and, as "ip:port", the peer's `local_addresses`, so
        it is not dialled again at the next start. `last_direct_ip`/`_port` follow
        the newest entry left, or fall back to unset when none is.
        """
        peer = self._peers.get(node_id)
        if peer is None:
            return False
        ip = normalise_ip(ip)
        changed = False

        kept = [e for e in peer.addresses
                if not (normalise_ip(e.get("ip") or "") == ip and (port is None or e.get("port") == port))]
        if len(kept) != len(peer.addresses):
            peer.addresses = kept
            changed = True

        advertised = peer.metadata.get(LOCAL_ADDRESSES_KEY)
        if isinstance(advertised, list):
            left = []
            for text in advertised:
                host, _, port_text = str(text).rpartition(":")
                if normalise_ip(host) == ip and (port is None or port_text == str(port)):
                    continue
                left.append(text)
            if len(left) != len(advertised):
                peer.metadata[LOCAL_ADDRESSES_KEY] = left
                changed = True

        if not changed:
            return False
        if peer.addresses:
            newest = max(peer.addresses, key=_seen_at)
            peer.last_direct_ip = newest["ip"]
            peer.last_direct_port = newest["port"]
        elif peer.last_direct_ip and normalise_ip(peer.last_direct_ip) == ip:
            peer.last_direct_ip = None
            peer.last_direct_port = DEFAULT_DIRECT_PORT
        self._save_cache()
        return True

    def get_peer(self, node_id: str) -> Optional[CachedPeer]:
        """
        Get cached peer information.

        Args:
            node_id: Peer's node ID

        Returns:
            CachedPeer if found, None otherwise
        """
        return self._peers.get(node_id)

    def get_all_peers(self) -> List[CachedPeer]:
        """Get all cached peers."""
        return list(self._peers.values())

    def get_recent_peers(self, hours: int = 24) -> List[CachedPeer]:
        """
        Get peers seen recently.

        Args:
            hours: Consider peers seen within this many hours

        Returns:
            List of recently seen peers
        """
        return [
            peer for peer in self._peers.values()
            if peer.is_recently_seen(hours)
        ]

    def get_peers_with_direct_connection(self) -> List[CachedPeer]:
        """Get peers that have Direct TLS connection info."""
        return [
            peer for peer in self._peers.values()
            if peer.supports_direct and (peer.addresses or peer.last_direct_ip)
        ]

    def remove_peer(self, node_id: str) -> bool:
        """
        Remove peer from cache.

        Args:
            node_id: Peer's node ID

        Returns:
            True if peer was removed
        """
        if node_id in self._peers:
            del self._peers[node_id]
            self._save_cache()
            return True
        return False

    def clear(self):
        """Clear all cached peers."""
        self._peers = {}
        self._save_cache()

    def cleanup_old_peers(self, days: int = 30):
        """
        Remove peers not seen for specified number of days.

        Args:
            days: Remove peers not seen for this many days
        """
        threshold = datetime.now(timezone.utc) - timedelta(days=days)
        removed_count = 0

        for node_id, peer in list(self._peers.items()):
            if peer.last_seen:
                try:
                    last_seen_dt = datetime.fromisoformat(peer.last_seen)
                    if last_seen_dt < threshold:
                        del self._peers[node_id]
                        removed_count += 1
                except Exception:
                    pass

        if removed_count > 0:
            logger.info("Cleaned up %d old peers", removed_count)
            self._save_cache()


# Self-test
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    print("Testing PeerCache...")

    with tempfile.TemporaryDirectory() as tmpdir:
        cache_file = Path(tmpdir) / "peers.json"
        cache = PeerCache(cache_file)

        # Test add peer
        cache.add_or_update_peer(
            node_id="dpc-node-alice-123",
            display_name="Alice",
            direct_ip="192.168.1.100",
            supports_direct=True,
            metadata={"skill": "python"}
        )
        print("[PASS] Add peer")

        # Test get peer
        peer = cache.get_peer("dpc-node-alice-123")
        assert peer is not None
        assert peer.display_name == "Alice"
        assert peer.last_direct_ip == "192.168.1.100"
        print("[PASS] Get peer")

        # Test update peer
        cache.add_or_update_peer(
            node_id="dpc-node-alice-123",
            display_name="Alice Updated",
            supports_webrtc=True,
            supports_direct=True  # Keep existing direct support
        )
        peer = cache.get_peer("dpc-node-alice-123")
        assert peer.display_name == "Alice Updated"
        assert peer.supports_webrtc == True
        assert peer.supports_direct == True  # Verify still supports direct
        print("[PASS] Update peer")

        # Test add multiple peers
        cache.add_or_update_peer(
            node_id="dpc-node-bob-456",
            display_name="Bob",
            direct_ip="192.168.1.101",
            supports_direct=True
        )

        all_peers = cache.get_all_peers()
        assert len(all_peers) == 2
        print("[PASS] Get all peers")

        # Test recent peers
        recent = cache.get_recent_peers(hours=24)
        assert len(recent) == 2
        print("[PASS] Get recent peers")

        # Test peers with direct connection
        direct_peers = cache.get_peers_with_direct_connection()
        assert len(direct_peers) == 2
        print("[PASS] Get peers with direct connection")

        # Test remove peer
        cache.remove_peer("dpc-node-bob-456")
        assert len(cache.get_all_peers()) == 1
        print("[PASS] Remove peer")

        # Test persistence
        cache2 = PeerCache(cache_file)
        assert len(cache2.get_all_peers()) == 1
        peer = cache2.get_peer("dpc-node-alice-123")
        assert peer.display_name == "Alice Updated"
        print("[PASS] Persistence")

        # Test clear
        cache.clear()
        assert len(cache.get_all_peers()) == 0
        print("[PASS] Clear cache")

    print("\n[PASS] All PeerCache tests passed!")
