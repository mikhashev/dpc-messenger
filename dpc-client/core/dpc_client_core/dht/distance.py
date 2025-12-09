"""
DHT Distance Utilities - XOR Distance Metric for Kademlia DHT

This module provides XOR distance calculation utilities for Kademlia DHT.
Node IDs are organized in a 128-bit key space using XOR distance metric.

Node ID Format: dpc-node-[32 hex chars] → 128-bit integer

Example:
    dpc-node-abcd1234abcd1234abcd1234abcd1234 → 0xabcd1234abcd1234abcd1234abcd1234
    dpc-node-1234abcd1234abcd1234abcd1234abcd → 0x1234abcd1234abcd1234abcd1234abcd
    distance = XOR of the two 128-bit integers
"""

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Node ID constants
NODE_ID_PREFIX = "dpc-node-"
NODE_ID_HEX_LENGTH = 32  # 32 hex chars = 128 bits (4 bits per hex char)
NODE_ID_BITS = 128  # Key space size


def parse_node_id(node_id: str) -> int:
    """
    Parse node ID string to 128-bit integer.

    Args:
        node_id: Node identifier (e.g., "dpc-node-abcd1234abcd1234abcd1234abcd1234")

    Returns:
        128-bit integer representation

    Raises:
        ValueError: If node ID format is invalid

    Example:
        >>> parse_node_id("dpc-node-abcd1234abcd1234abcd1234abcd1234")
        228826127032234627312174085396624261684
    """
    if not node_id.startswith(NODE_ID_PREFIX):
        raise ValueError(f"Node ID must start with '{NODE_ID_PREFIX}': {node_id}")

    hex_part = node_id[len(NODE_ID_PREFIX):]

    if len(hex_part) != NODE_ID_HEX_LENGTH:
        raise ValueError(
            f"Node ID hex part must be {NODE_ID_HEX_LENGTH} characters: {node_id} "
            f"(got {len(hex_part)})"
        )

    try:
        return int(hex_part, 16)
    except ValueError as e:
        raise ValueError(f"Invalid hex characters in node ID: {node_id}") from e


def xor_distance(node_id_a: str, node_id_b: str) -> int:
    """
    Compute XOR distance between two node IDs.

    Kademlia uses XOR distance to organize nodes into a logarithmic overlay.
    Distance is symmetric: d(A, B) = d(B, A)

    Args:
        node_id_a: First node identifier
        node_id_b: Second node identifier

    Returns:
        XOR distance as integer (0 to 2^128 - 1)

    Example:
        >>> xor_distance("dpc-node-abcd1234abcd1234abcd1234abcd1234", "dpc-node-1234abcd1234abcd1234abcd1234abcd")
        198294774853351167490970896651327676241
    """
    a_int = parse_node_id(node_id_a)
    b_int = parse_node_id(node_id_b)
    return a_int ^ b_int


def bucket_index(distance: int) -> int:
    """
    Determine k-bucket index from XOR distance.

    Kademlia routing table has 128 k-buckets (for 128-bit key space).
    Bucket index = floor(log2(distance))

    Args:
        distance: XOR distance between node IDs (must be > 0)

    Returns:
        Bucket index (0-127)

    Example:
        >>> bucket_index(1)  # 2^0
        0
        >>> bucket_index(5)  # 2^2 < 5 < 2^3
        2
        >>> bucket_index(256)  # 2^8
        8
    """
    if distance == 0:
        raise ValueError("Distance cannot be 0 (nodes with same ID)")

    # bit_length() returns position of highest set bit (1-indexed)
    # Subtract 1 to get bucket index (0-indexed)
    index = distance.bit_length() - 1

    # Clamp to valid range [0, 127]
    return min(index, NODE_ID_BITS - 1)


def node_id_distance_to_bucket(node_id_a: str, node_id_b: str) -> int:
    """
    Helper function: Compute bucket index for two node IDs.

    Args:
        node_id_a: First node identifier
        node_id_b: Second node identifier

    Returns:
        Bucket index (0-127)

    Raises:
        ValueError: If nodes have same ID

    Example:
        >>> node_id_distance_to_bucket("dpc-node-0000000000000001", "dpc-node-0000000000000002")
        0
    """
    distance = xor_distance(node_id_a, node_id_b)
    if distance == 0:
        raise ValueError(f"Nodes have same ID: {node_id_a}")
    return bucket_index(distance)


def sort_by_distance(target_id: str, node_ids: List[str]) -> List[str]:
    """
    Sort node IDs by XOR distance to target (ascending order).

    Used in Kademlia lookup to find k closest nodes.

    Args:
        target_id: Target node identifier
        node_ids: List of node identifiers to sort

    Returns:
        Sorted list (closest first)

    Example:
        >>> nodes = ["dpc-node-0000000000000003", "dpc-node-0000000000000001"]
        >>> sort_by_distance("dpc-node-0000000000000000", nodes)
        ['dpc-node-0000000000000001', 'dpc-node-0000000000000003']
    """
    return sorted(node_ids, key=lambda nid: xor_distance(target_id, nid))


def find_closest_nodes(
    target_id: str,
    candidates: List[Tuple[str, any]],
    count: int = 20
) -> List[Tuple[str, any]]:
    """
    Find k closest nodes to target from candidate list.

    Args:
        target_id: Target node identifier
        candidates: List of (node_id, data) tuples
        count: Number of closest nodes to return (k parameter)

    Returns:
        List of (node_id, data) tuples sorted by distance (closest first)

    Example:
        >>> candidates = [("dpc-node-0000000000000003", "data1"),
        ...               ("dpc-node-0000000000000001", "data2")]
        >>> find_closest_nodes("dpc-node-0000000000000000", candidates, count=1)
        [('dpc-node-0000000000000001', 'data2')]
    """
    sorted_candidates = sorted(
        candidates,
        key=lambda item: xor_distance(target_id, item[0])
    )
    return sorted_candidates[:count]


def is_closer(target_id: str, candidate_id: str, reference_id: str) -> bool:
    """
    Check if candidate is closer to target than reference.

    Used to determine if iterative lookup found closer nodes.

    Args:
        target_id: Target node identifier
        candidate_id: Candidate node identifier
        reference_id: Reference node identifier to compare against

    Returns:
        True if candidate is closer to target than reference

    Example:
        >>> is_closer("dpc-node-0000000000000000",
        ...           "dpc-node-0000000000000001",
        ...           "dpc-node-0000000000000003")
        True
    """
    candidate_dist = xor_distance(target_id, candidate_id)
    reference_dist = xor_distance(target_id, reference_id)
    return candidate_dist < reference_dist


def generate_random_node_id_in_bucket(reference_id: str, bucket_idx: int) -> str:
    """
    Generate random node ID that falls in specified bucket relative to reference.

    Used for routing table refresh (generate random IDs to query).

    Args:
        reference_id: Reference node identifier (own node ID)
        bucket_idx: Target bucket index (0-127)

    Returns:
        Random node ID in specified bucket

    Example:
        >>> random_id = generate_random_node_id_in_bucket("dpc-node-00000000000000000000000000000000", 5)
        >>> # random_id will have distance in range [2^5, 2^6) from reference
    """
    import random

    # Generate random distance in bucket range [2^bucket_idx, 2^(bucket_idx+1))
    min_distance = 2 ** bucket_idx
    max_distance = 2 ** (bucket_idx + 1) - 1
    random_distance = random.randint(min_distance, max_distance)

    # XOR with reference to get node ID
    reference_int = parse_node_id(reference_id)
    random_int = reference_int ^ random_distance

    # Convert back to node ID string (32 hex chars, zero-padded)
    hex_part = f"{random_int:032x}"
    return f"{NODE_ID_PREFIX}{hex_part}"
