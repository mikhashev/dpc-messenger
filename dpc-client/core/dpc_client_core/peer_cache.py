# dpc-client/core/dpc_client_core/peer_cache.py

import json
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict


@dataclass
class CachedPeer:
    """Information about a known peer."""
    node_id: str
    display_name: Optional[str] = None
    last_seen: Optional[str] = None  # ISO format timestamp
    last_direct_ip: Optional[str] = None  # For Direct TLS fallback
    last_direct_port: int = 8888
    supports_webrtc: bool = False
    supports_direct: bool = False
    metadata: Dict[str, Any] = None  # Additional peer metadata

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}

    def is_recently_seen(self, hours: int = 24) -> bool:
        """Check if peer was seen recently."""
        if not self.last_seen:
            return False
        try:
            last_seen_dt = datetime.fromisoformat(self.last_seen)
            threshold = datetime.utcnow() - timedelta(hours=hours)
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
            print(f"Peer cache not found at {self.cache_file}, starting fresh")
            return

        try:
            with open(self.cache_file, 'r') as f:
                data = json.load(f)

            peers_data = data.get("peers", [])
            self._peers = {}

            for peer_dict in peers_data:
                peer = CachedPeer(**peer_dict)
                self._peers[peer.node_id] = peer

            print(f"[OK] Loaded {len(self._peers)} peers from cache")

        except Exception as e:
            print(f"Error loading peer cache: {e}")
            self._peers = {}

    def _save_cache(self):
        """Save peer cache to disk."""
        try:
            # Ensure directory exists
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)

            # Convert peers to dict
            peers_data = [asdict(peer) for peer in self._peers.values()]

            data = {
                "version": "1.0",
                "last_updated": datetime.utcnow().isoformat(),
                "peers": peers_data
            }

            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)

            print(f"[OK] Saved {len(self._peers)} peers to cache")

        except Exception as e:
            print(f"Error saving peer cache: {e}")

    def add_or_update_peer(
        self,
        node_id: str,
        display_name: Optional[str] = None,
        direct_ip: Optional[str] = None,
        direct_port: int = 8888,
        supports_webrtc: bool = False,
        supports_direct: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Add or update peer information.

        Args:
            node_id: Peer's node ID
            display_name: Peer's display name
            direct_ip: Last known IP for Direct TLS
            direct_port: Direct TLS port
            supports_webrtc: Whether peer supports WebRTC
            supports_direct: Whether peer supports Direct TLS
            metadata: Additional metadata
        """
        if node_id in self._peers:
            # Update existing peer
            peer = self._peers[node_id]
            if display_name:
                peer.display_name = display_name
            if direct_ip:
                peer.last_direct_ip = direct_ip
                peer.last_direct_port = direct_port
            peer.supports_webrtc = supports_webrtc
            peer.supports_direct = supports_direct
            peer.last_seen = datetime.utcnow().isoformat()
            if metadata:
                peer.metadata.update(metadata)
        else:
            # Create new peer
            peer = CachedPeer(
                node_id=node_id,
                display_name=display_name,
                last_seen=datetime.utcnow().isoformat(),
                last_direct_ip=direct_ip,
                last_direct_port=direct_port,
                supports_webrtc=supports_webrtc,
                supports_direct=supports_direct,
                metadata=metadata or {}
            )
            self._peers[node_id] = peer

        self._save_cache()

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
            if peer.supports_direct and peer.last_direct_ip
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
        threshold = datetime.utcnow() - timedelta(days=days)
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
            print(f"[OK] Cleaned up {removed_count} old peers")
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
