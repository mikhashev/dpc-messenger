"""
Vector Clock - Causality Tracking for Gossip Protocol

Implements vector clocks for detecting causality and conflicts in distributed
gossip messages. Essential for anti-entropy sync and eventual consistency.

Key Concepts:
- Each node maintains a vector of timestamps (one per node)
- Incremented on local events
- Merged on message receipt
- Enables detection of: causality (happens-before), concurrency, conflicts

Example:
    >>> clock = VectorClock("dpc-node-alice")
    >>> clock.increment()  # Local event
    >>> clock.clock
    {'dpc-node-alice': 1}
    >>>
    >>> other = VectorClock("dpc-node-bob")
    >>> other.increment()
    >>> clock.merge(other)  # Received message from Bob
    >>> clock.clock
    {'dpc-node-alice': 1, 'dpc-node-bob': 1}
"""

from typing import Dict


class VectorClock:
    """
    Vector clock for distributed causality tracking.

    Maintains a vector of logical timestamps, one per node in the system.
    Used to determine causal ordering of events in gossip protocol.

    Attributes:
        node_id: This node's identifier
        clock: Dict mapping node_id -> timestamp

    Example:
        >>> alice = VectorClock("dpc-node-alice")
        >>> alice.increment()
        >>> alice.clock
        {'dpc-node-alice': 1}
        >>>
        >>> bob = VectorClock("dpc-node-bob")
        >>> bob.increment()
        >>> bob.increment()
        >>> bob.clock
        {'dpc-node-bob': 2}
        >>>
        >>> alice.merge(bob)
        >>> alice.clock
        {'dpc-node-alice': 1, 'dpc-node-bob': 2}
    """

    def __init__(self, node_id: str):
        """
        Initialize vector clock.

        Args:
            node_id: This node's identifier
        """
        self.node_id = node_id
        self.clock: Dict[str, int] = {}

    def increment(self):
        """
        Increment this node's timestamp (local event).

        Called on:
        - Sending a message
        - Local state change

        Example:
            >>> clock = VectorClock("dpc-node-alice")
            >>> clock.increment()
            >>> clock.clock['dpc-node-alice']
            1
        """
        self.clock[self.node_id] = self.clock.get(self.node_id, 0) + 1

    def merge(self, other: "VectorClock"):
        """
        Merge with another vector clock (element-wise max).

        Called when receiving a message from another node.
        Updates our clock to reflect knowledge of other's events.

        Args:
            other: Vector clock from received message

        Example:
            >>> alice = VectorClock("dpc-node-alice")
            >>> alice.clock = {'dpc-node-alice': 5, 'dpc-node-bob': 3}
            >>> bob = VectorClock("dpc-node-bob")
            >>> bob.clock = {'dpc-node-alice': 4, 'dpc-node-bob': 7}
            >>> alice.merge(bob)
            >>> alice.clock
            {'dpc-node-alice': 5, 'dpc-node-bob': 7}
        """
        # Take element-wise maximum
        for node, timestamp in other.clock.items():
            self.clock[node] = max(self.clock.get(node, 0), timestamp)

    def happens_before(self, other: "VectorClock") -> bool:
        """
        Check if this clock happens before other (strict causality).

        Returns True if:
        - All our timestamps <= other's timestamps
        - At least one timestamp is strictly less

        Args:
            other: Vector clock to compare

        Returns:
            True if this happens before other

        Example:
            >>> a = VectorClock("alice")
            >>> a.clock = {'alice': 1, 'bob': 2}
            >>> b = VectorClock("bob")
            >>> b.clock = {'alice': 2, 'bob': 3}
            >>> a.happens_before(b)
            True
            >>> b.happens_before(a)
            False
        """
        # All our timestamps must be <= other's
        all_nodes = set(self.clock.keys()) | set(other.clock.keys())

        has_strictly_less = False
        for node in all_nodes:
            our_ts = self.clock.get(node, 0)
            their_ts = other.clock.get(node, 0)

            if our_ts > their_ts:
                return False  # We're ahead in at least one dimension

            if our_ts < their_ts:
                has_strictly_less = True

        return has_strictly_less

    def concurrent_with(self, other: "VectorClock") -> bool:
        """
        Check if this clock is concurrent with other (no causal relationship).

        Returns True if neither happens before the other.
        Indicates potential conflict that needs resolution.

        Args:
            other: Vector clock to compare

        Returns:
            True if clocks are concurrent

        Example:
            >>> a = VectorClock("alice")
            >>> a.clock = {'alice': 5, 'bob': 1}
            >>> b = VectorClock("bob")
            >>> b.clock = {'alice': 1, 'bob': 5}
            >>> a.concurrent_with(b)
            True
        """
        return not self.happens_before(other) and not other.happens_before(self)

    def equals(self, other: "VectorClock") -> bool:
        """
        Check if clocks are equal (all timestamps match).

        Args:
            other: Vector clock to compare

        Returns:
            True if clocks are identical

        Example:
            >>> a = VectorClock("alice")
            >>> a.clock = {'alice': 3, 'bob': 2}
            >>> b = VectorClock("bob")
            >>> b.clock = {'alice': 3, 'bob': 2}
            >>> a.equals(b)
            True
        """
        all_nodes = set(self.clock.keys()) | set(other.clock.keys())

        for node in all_nodes:
            if self.clock.get(node, 0) != other.clock.get(node, 0):
                return False

        return True

    def get_timestamp(self, node_id: str) -> int:
        """
        Get timestamp for specific node.

        Args:
            node_id: Node identifier

        Returns:
            Timestamp for node (0 if not present)
        """
        return self.clock.get(node_id, 0)

    def to_dict(self) -> Dict[str, int]:
        """Serialize to dictionary (for protocol messages)."""
        return dict(self.clock)

    @classmethod
    def from_dict(cls, node_id: str, clock_dict: Dict[str, int]) -> "VectorClock":
        """
        Deserialize from dictionary.

        Args:
            node_id: This node's identifier
            clock_dict: Dictionary of node_id -> timestamp

        Returns:
            VectorClock instance
        """
        vc = cls(node_id)
        vc.clock = dict(clock_dict)
        return vc

    def copy(self) -> "VectorClock":
        """Create a copy of this vector clock."""
        vc = VectorClock(self.node_id)
        vc.clock = dict(self.clock)
        return vc

    def __repr__(self) -> str:
        """String representation for debugging."""
        return f"<VectorClock node={self.node_id[:20]} clock={self.clock}>"

    def __str__(self) -> str:
        """Human-readable string representation."""
        return str(self.clock)
