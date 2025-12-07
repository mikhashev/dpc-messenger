"""
Peer Endpoint Model - Enhanced DHT Storage Schema (Phase 6)

This module defines the enhanced peer endpoint information structure that
supports IPv4, IPv6, relay nodes, and hole punching metadata for the 6-tier
connection fallback hierarchy.

Schema Version 2.0 (Phase 6):
- IPv4: local + external addresses, NAT type detection
- IPv6: global IPv6 addresses (no NAT)
- Relay: Volunteer relay node availability
- Punch: UDP hole punching metadata
"""

import json
import time
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any


@dataclass
class IPv4Info:
    """IPv4 connection information."""
    local: str  # Local address (e.g., "192.168.1.100:8888")
    external: Optional[str] = None  # External address after NAT (e.g., "203.0.113.50:12345")
    nat_type: Optional[str] = None  # "none" | "cone" | "symmetric" | "unknown"


@dataclass
class IPv6Info:
    """IPv6 connection information."""
    address: str  # IPv6 address with port (e.g., "[2001:db8::1]:8888")
    type: str = "global"  # "global" | "ula" | "link-local"


@dataclass
class RelayInfo:
    """Volunteer relay node information."""
    available: bool = False  # Is this node volunteering as a relay?
    max_peers: int = 0  # Maximum concurrent relayed connections
    region: str = "global"  # Preferred region (for latency optimization)
    uptime: float = 0.0  # Uptime percentage (0.0-1.0)


@dataclass
class PunchInfo:
    """UDP hole punching metadata."""
    supported: bool = False  # Can this node perform hole punching?
    stun_port: Optional[int] = None  # UDP port for hole punching (usually TLS port + 1)
    success_rate: float = 0.0  # Historical success rate (0.0-1.0)


@dataclass
class PeerEndpoint:
    """
    Enhanced peer endpoint information for Phase 6 fallback logic.

    This structure stores all connection methods available for a peer:
    - IPv6 direct (if available) - Priority 1
    - IPv4 direct - Priority 2
    - Hub WebRTC (checked separately) - Priority 3
    - UDP hole punching - Priority 4
    - Volunteer relay - Priority 5
    - Gossip (always available) - Priority 6

    Attributes:
        schema_version: Schema version for backward compatibility (2.0)
        node_id: Peer's node identifier
        ipv4: IPv4 connection information
        ipv6: IPv6 connection information (optional)
        relay: Relay node information (optional)
        punch: Hole punching metadata (optional)
        timestamp: Last update timestamp (UNIX epoch)

    Example:
        >>> endpoint = PeerEndpoint(
        ...     node_id="dpc-node-abc123",
        ...     ipv4=IPv4Info(local="192.168.1.100:8888", external="203.0.113.50:12345", nat_type="cone"),
        ...     ipv6=IPv6Info(address="[2001:db8::1]:8888"),
        ...     relay=RelayInfo(available=True, max_peers=10),
        ...     punch=PunchInfo(supported=True, stun_port=8889)
        ... )
        >>> endpoint.to_json()
        '{"schema_version": "2.0", "node_id": "dpc-node-abc123", ...}'
    """

    schema_version: str  # "2.0" for Phase 6
    node_id: str  # dpc-node-*
    ipv4: IPv4Info
    ipv6: Optional[IPv6Info] = None
    relay: Optional[RelayInfo] = None
    punch: Optional[PunchInfo] = None
    timestamp: float = 0.0  # UNIX timestamp

    def __post_init__(self):
        """Set timestamp if not provided."""
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """
        Convert to dictionary for DHT storage.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        result = {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "ipv4": {
                "local": self.ipv4.local,
            },
            "timestamp": self.timestamp,
        }

        # Optional IPv4 fields
        if self.ipv4.external:
            result["ipv4"]["external"] = self.ipv4.external
        if self.ipv4.nat_type:
            result["ipv4"]["nat_type"] = self.ipv4.nat_type

        # Optional IPv6
        if self.ipv6:
            result["ipv6"] = {
                "address": self.ipv6.address,
                "type": self.ipv6.type,
            }

        # Optional relay info
        if self.relay and self.relay.available:
            result["relay"] = {
                "available": True,
                "max_peers": self.relay.max_peers,
                "region": self.relay.region,
                "uptime": self.relay.uptime,
            }

        # Optional punch info
        if self.punch and self.punch.supported:
            result["punch"] = {
                "supported": True,
                "stun_port": self.punch.stun_port,
                "success_rate": self.punch.success_rate,
            }

        return result

    def to_json(self) -> str:
        """
        Convert to JSON string for DHT storage.

        Returns:
            JSON string representation
        """
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PeerEndpoint":
        """
        Parse from dictionary (DHT FIND_VALUE response).

        Args:
            data: Dictionary from DHT storage

        Returns:
            PeerEndpoint instance

        Raises:
            ValueError: If required fields are missing
        """
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict, got {type(data).__name__}")

        schema_version = data.get("schema_version", "1.0")
        node_id = data.get("node_id")

        if not node_id:
            raise ValueError("Missing required field: node_id")

        # Parse IPv4 (required)
        ipv4_data = data.get("ipv4")
        if not ipv4_data or "local" not in ipv4_data:
            raise ValueError("Missing required field: ipv4.local")

        ipv4 = IPv4Info(
            local=ipv4_data["local"],
            external=ipv4_data.get("external"),
            nat_type=ipv4_data.get("nat_type"),
        )

        # Parse IPv6 (optional)
        ipv6 = None
        if "ipv6" in data:
            ipv6_data = data["ipv6"]
            ipv6 = IPv6Info(
                address=ipv6_data["address"],
                type=ipv6_data.get("type", "global"),
            )

        # Parse relay (optional)
        relay = None
        if "relay" in data and data["relay"].get("available"):
            relay_data = data["relay"]
            relay = RelayInfo(
                available=True,
                max_peers=relay_data.get("max_peers", 0),
                region=relay_data.get("region", "global"),
                uptime=relay_data.get("uptime", 0.0),
            )

        # Parse punch (optional)
        punch = None
        if "punch" in data and data["punch"].get("supported"):
            punch_data = data["punch"]
            punch = PunchInfo(
                supported=True,
                stun_port=punch_data.get("stun_port"),
                success_rate=punch_data.get("success_rate", 0.0),
            )

        return cls(
            schema_version=schema_version,
            node_id=node_id,
            ipv4=ipv4,
            ipv6=ipv6,
            relay=relay,
            punch=punch,
            timestamp=data.get("timestamp", time.time()),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "PeerEndpoint":
        """
        Parse from JSON string (DHT FIND_VALUE response).

        Args:
            json_str: JSON string from DHT storage

        Returns:
            PeerEndpoint instance

        Raises:
            ValueError: If JSON is invalid or required fields are missing
        """
        try:
            data = json.loads(json_str)
            return cls.from_dict(data)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

    @classmethod
    def from_legacy_string(cls, node_id: str, value: str) -> "PeerEndpoint":
        """
        Parse from legacy "ip:port" format (backward compatibility).

        Args:
            node_id: Node identifier
            value: Legacy "ip:port" string

        Returns:
            PeerEndpoint instance with basic IPv4 info

        Example:
            >>> endpoint = PeerEndpoint.from_legacy_string(
            ...     "dpc-node-abc123",
            ...     "192.168.1.100:8888"
            ... )
            >>> endpoint.ipv4.local
            '192.168.1.100:8888'
        """
        if ":" not in value:
            raise ValueError(f"Invalid legacy format (expected 'ip:port'): {value}")

        return cls(
            schema_version="1.0",  # Legacy format
            node_id=node_id,
            ipv4=IPv4Info(local=value),
            timestamp=time.time(),
        )

    def has_ipv6(self) -> bool:
        """Check if peer has IPv6 connectivity."""
        return self.ipv6 is not None

    def supports_relay(self) -> bool:
        """Check if peer volunteers as a relay node."""
        return self.relay is not None and self.relay.available

    def supports_hole_punching(self) -> bool:
        """Check if peer supports UDP hole punching."""
        return self.punch is not None and self.punch.supported

    def get_primary_address(self) -> tuple[str, int]:
        """
        Get primary connection address (for backward compatibility).

        Returns:
            (ip, port) tuple from IPv4 local address
        """
        ip, port_str = self.ipv4.local.rsplit(":", 1)
        return (ip, int(port_str))
