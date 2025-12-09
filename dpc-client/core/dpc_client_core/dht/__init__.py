"""
DHT (Distributed Hash Table) - Kademlia Implementation

This package implements a complete Kademlia DHT for decentralized peer discovery.

Modules:
- distance: XOR distance utilities for Kademlia metric space
- routing: Routing table with 128 k-buckets for O(log n) lookups
- rpc: UDP-based RPC handler (PING, FIND_NODE, STORE, FIND_VALUE)
- manager: High-level DHT orchestration (bootstrap, iterative lookup, announce)

Main exports:
- DHTManager: Complete DHT coordinator (use this for most cases)
- RoutingTable, DHTNode: Routing table and node representation
- DHTRPCHandler, RPCConfig: Low-level RPC interface
- xor_distance, bucket_index: Distance metric utilities
"""

# Core DHT components
from .distance import (
    xor_distance,
    bucket_index,
    parse_node_id,
    sort_by_distance,
    generate_random_node_id_in_bucket,
    NODE_ID_BITS,
    NODE_ID_HEX_LENGTH,
    NODE_ID_PREFIX,
)

from .routing import (
    DHTNode,
    KBucket,
    RoutingTable,
)

from .rpc import (
    DHTRPCHandler,
    DHTProtocol,
    RPCConfig,
)

from .manager import (
    DHTManager,
    DHTConfig,
)

__all__ = [
    # Manager (high-level API)
    "DHTManager",
    "DHTConfig",

    # Routing
    "DHTNode",
    "KBucket",
    "RoutingTable",

    # RPC
    "DHTRPCHandler",
    "DHTProtocol",
    "RPCConfig",

    # Distance utilities
    "xor_distance",
    "bucket_index",
    "parse_node_id",
    "sort_by_distance",
    "generate_random_node_id_in_bucket",
    "NODE_ID_BITS",
    "NODE_ID_HEX_LENGTH",
    "NODE_ID_PREFIX",
]
