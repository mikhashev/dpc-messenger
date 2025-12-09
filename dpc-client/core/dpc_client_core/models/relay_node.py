"""
Relay Node Model - Volunteer Relay Metadata

Data structures for representing volunteer relay nodes in the DHT.
Relays provide fallback connectivity for symmetric NAT/CGNAT scenarios
where direct connections and hole punching fail.

Key Features:
- Voluntary opt-in (config setting)
- Privacy-preserving (forwards encrypted messages)
- Capacity limits (max peers, bandwidth)
- Quality metrics (uptime, latency)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class RelayNode:
    """
    Metadata for a volunteer relay node.

    Attributes:
        node_id: Relay node identifier
        ip: Relay IP address
        port: Relay port
        available: Whether relay is accepting new sessions
        max_peers: Maximum concurrent peer sessions
        current_peers: Current number of active sessions
        region: Geographic region (e.g., "us-west", "eu-central", "global")
        uptime: Uptime score (0.0-1.0, higher is better)
        latency_ms: Average latency in milliseconds
        bandwidth_mbps: Bandwidth limit in Mbps
        discovered_at: UNIX timestamp when relay was discovered

    Example:
        >>> relay = RelayNode(
        ...     node_id="dpc-node-relay-abc123",
        ...     ip="203.0.113.50",
        ...     port=8888,
        ...     available=True,
        ...     max_peers=10,
        ...     current_peers=3,
        ...     uptime=0.98
        ... )
        >>> relay.capacity_score()
        0.7
    """

    node_id: str
    ip: str
    port: int
    available: bool
    max_peers: int
    current_peers: int = 0
    region: str = "global"
    uptime: float = 1.0  # 0.0-1.0
    latency_ms: float = 100.0
    bandwidth_mbps: float = 10.0
    discovered_at: float = 0.0

    def capacity_score(self) -> float:
        """
        Calculate available capacity score (0.0-1.0).

        Returns:
            1.0 = no peers, 0.0 = full capacity

        Example:
            >>> relay.current_peers = 3
            >>> relay.max_peers = 10
            >>> relay.capacity_score()
            0.7
        """
        if self.max_peers == 0:
            return 0.0

        return 1.0 - (self.current_peers / self.max_peers)

    def quality_score(self) -> float:
        """
        Calculate overall quality score (0.0-1.0).

        Combines:
        - Uptime (50% weight)
        - Capacity (30% weight)
        - Latency (20% weight, normalized to 0-500ms range)

        Returns:
            Composite quality score (higher is better)

        Example:
            >>> relay.uptime = 0.95
            >>> relay.current_peers = 2
            >>> relay.max_peers = 10
            >>> relay.latency_ms = 50
            >>> relay.quality_score()
            0.855
        """
        # Uptime component (50% weight)
        uptime_component = self.uptime * 0.5

        # Capacity component (30% weight)
        capacity_component = self.capacity_score() * 0.3

        # Latency component (20% weight)
        # Normalize latency: 0ms = 1.0, 500ms = 0.0, clamped
        latency_normalized = max(0.0, min(1.0, 1.0 - (self.latency_ms / 500.0)))
        latency_component = latency_normalized * 0.2

        return uptime_component + capacity_component + latency_component

    def is_full(self) -> bool:
        """Check if relay is at capacity."""
        return self.current_peers >= self.max_peers

    def to_dict(self) -> dict:
        """Serialize to dictionary for DHT storage."""
        return {
            "node_id": self.node_id,
            "ip": self.ip,
            "port": self.port,
            "available": self.available,
            "max_peers": self.max_peers,
            "current_peers": self.current_peers,
            "region": self.region,
            "uptime": self.uptime,
            "latency_ms": self.latency_ms,
            "bandwidth_mbps": self.bandwidth_mbps,
        }

    @classmethod
    def from_dict(cls, data: dict, discovered_at: float = 0.0) -> "RelayNode":
        """
        Deserialize from dictionary.

        Args:
            data: Dictionary from DHT storage
            discovered_at: Timestamp when relay was discovered

        Returns:
            RelayNode instance
        """
        return cls(
            node_id=data["node_id"],
            ip=data["ip"],
            port=data["port"],
            available=data.get("available", True),
            max_peers=data.get("max_peers", 10),
            current_peers=data.get("current_peers", 0),
            region=data.get("region", "global"),
            uptime=data.get("uptime", 1.0),
            latency_ms=data.get("latency_ms", 100.0),
            bandwidth_mbps=data.get("bandwidth_mbps", 10.0),
            discovered_at=discovered_at,
        )


@dataclass
class RelaySession:
    """
    Active relay session metadata.

    Tracks a relayed connection between two peers through a relay node.

    Attributes:
        session_id: Unique session identifier
        relay_node_id: Relay node handling this session
        peer_a_id: First peer node ID
        peer_b_id: Second peer node ID
        created_at: Session creation timestamp
        last_activity: Last message timestamp
        messages_relayed: Number of messages forwarded
        bytes_relayed: Total bytes forwarded
    """

    session_id: str
    relay_node_id: str
    peer_a_id: str
    peer_b_id: str
    created_at: float
    last_activity: float
    messages_relayed: int = 0
    bytes_relayed: int = 0

    def is_stale(self, timeout: float = 300.0) -> bool:
        """
        Check if session is stale (no activity).

        Args:
            timeout: Inactivity timeout in seconds (default 5 minutes)

        Returns:
            True if session has been inactive for longer than timeout
        """
        import time
        return (time.time() - self.last_activity) > timeout
