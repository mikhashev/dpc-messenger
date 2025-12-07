"""
Gossip Message - Store-and-Forward Message Model

Data structure for gossip messages in epidemic routing protocol.
Used for disaster-resilient messaging when all direct methods fail.

Key Features:
- Multi-hop message routing
- TTL and hop limits prevent infinite forwarding
- Vector clocks for causality tracking
- Deduplication via message IDs
- Tracking of forwarding history

Example:
    >>> msg = GossipMessage(
    ...     id="msg-abc123",
    ...     source="dpc-node-alice",
    ...     destination="dpc-node-bob",
    ...     payload={"command": "HELLO"},
    ...     hops=0,
    ...     max_hops=5
    ... )
    >>> msg.can_forward()
    True
    >>> msg.increment_hops("dpc-node-relay1")
    >>> msg.hops
    1
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .vector_clock import VectorClock


@dataclass
class GossipMessage:
    """
    Gossip protocol message for store-and-forward routing.

    Attributes:
        id: Unique message identifier (UUID)
        source: Source node ID (original sender)
        destination: Destination node ID (final recipient)
        payload: Message payload (encrypted)
        hops: Current number of hops
        max_hops: Maximum allowed hops (TTL)
        already_forwarded: List of nodes that have forwarded this message
        vector_clock: Causality tracking clock
        created_at: UNIX timestamp when message was created
        ttl: Time-to-live in seconds (expiry)
        priority: Message priority ("normal", "high", "low")

    Example:
        >>> msg = GossipMessage(
        ...     id="msg-123",
        ...     source="alice",
        ...     destination="bob",
        ...     payload={"text": "Hello"},
        ...     hops=2,
        ...     max_hops=5,
        ...     already_forwarded=["alice", "relay1", "relay2"]
        ... )
        >>> msg.can_forward()
        True
        >>> msg.is_expired()
        False
    """

    id: str
    source: str
    destination: str
    payload: Dict
    hops: int
    max_hops: int
    already_forwarded: List[str] = field(default_factory=list)
    vector_clock: Dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    ttl: int = 86400  # 24 hours default
    priority: str = "normal"

    def can_forward(self) -> bool:
        """
        Check if message can be forwarded.

        Returns:
            True if hops < max_hops and not expired

        Example:
            >>> msg = GossipMessage("id", "alice", "bob", {}, 4, 5)
            >>> msg.can_forward()
            True
            >>> msg.hops = 5
            >>> msg.can_forward()
            False
        """
        return self.hops < self.max_hops and not self.is_expired()

    def increment_hops(self, forwarder_id: str):
        """
        Increment hop count and add forwarder to history.

        Args:
            forwarder_id: Node ID of forwarder

        Example:
            >>> msg = GossipMessage("id", "alice", "bob", {}, 0, 5)
            >>> msg.increment_hops("relay1")
            >>> msg.hops
            1
            >>> msg.already_forwarded
            ['relay1']
        """
        self.hops += 1
        if forwarder_id not in self.already_forwarded:
            self.already_forwarded.append(forwarder_id)

    def is_expired(self) -> bool:
        """
        Check if message has expired (TTL exceeded).

        Returns:
            True if current time > created_at + ttl

        Example:
            >>> msg = GossipMessage("id", "alice", "bob", {}, 0, 5)
            >>> msg.ttl = 1  # 1 second
            >>> msg.created_at = time.time() - 10  # 10 seconds ago
            >>> msg.is_expired()
            True
        """
        return time.time() > (self.created_at + self.ttl)

    def already_seen_by(self, node_id: str) -> bool:
        """
        Check if node has already forwarded this message.

        Args:
            node_id: Node to check

        Returns:
            True if node is in already_forwarded list

        Example:
            >>> msg = GossipMessage("id", "alice", "bob", {}, 1, 5)
            >>> msg.already_forwarded = ["alice", "relay1"]
            >>> msg.already_seen_by("relay1")
            True
            >>> msg.already_seen_by("relay2")
            False
        """
        return node_id in self.already_forwarded

    def to_dict(self) -> Dict:
        """
        Serialize to dictionary for protocol transmission.

        Returns:
            Dictionary representation
        """
        return {
            "id": self.id,
            "source": self.source,
            "destination": self.destination,
            "payload": self.payload,
            "hops": self.hops,
            "max_hops": self.max_hops,
            "already_forwarded": self.already_forwarded,
            "vector_clock": self.vector_clock,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "GossipMessage":
        """
        Deserialize from dictionary.

        Args:
            data: Dictionary from protocol message

        Returns:
            GossipMessage instance
        """
        return cls(
            id=data["id"],
            source=data["source"],
            destination=data["destination"],
            payload=data["payload"],
            hops=data["hops"],
            max_hops=data["max_hops"],
            already_forwarded=data.get("already_forwarded", []),
            vector_clock=data.get("vector_clock", {}),
            created_at=data.get("created_at", time.time()),
            ttl=data.get("ttl", 86400),
            priority=data.get("priority", "normal"),
        )

    @classmethod
    def create(
        cls,
        source: str,
        destination: str,
        payload: Dict,
        max_hops: int = 5,
        ttl: int = 86400,
        priority: str = "normal",
        vector_clock: Optional[Dict[str, int]] = None
    ) -> "GossipMessage":
        """
        Create a new gossip message.

        Args:
            source: Source node ID
            destination: Destination node ID
            payload: Message payload
            max_hops: Maximum hops allowed (default 5)
            ttl: Time-to-live in seconds (default 24 hours)
            priority: Message priority ("normal", "high", "low")
            vector_clock: Initial vector clock (optional)

        Returns:
            New GossipMessage instance

        Example:
            >>> msg = GossipMessage.create(
            ...     "alice",
            ...     "bob",
            ...     {"text": "Hello"},
            ...     max_hops=5
            ... )
            >>> msg.hops
            0
            >>> msg.id.startswith("msg-")
            True
        """
        return cls(
            id=f"msg-{uuid.uuid4()}",
            source=source,
            destination=destination,
            payload=payload,
            hops=0,
            max_hops=max_hops,
            already_forwarded=[source],  # Source is first forwarder
            vector_clock=vector_clock or {},
            created_at=time.time(),
            ttl=ttl,
            priority=priority,
        )

    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"<GossipMessage id={self.id} "
            f"src={self.source[:20]} dst={self.destination[:20]} "
            f"hops={self.hops}/{self.max_hops}>"
        )
