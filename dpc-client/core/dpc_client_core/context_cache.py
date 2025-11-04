# dpc-client/core/dpc_client_core/context_cache.py

from typing import Dict

# We need to import this to use it as a type hint
from dpc_protocol.pcm_core import PersonalContext

class ContextCache:
    """
    A simple in-memory cache for storing personal contexts received from peers.
    """
    def __init__(self):
        self._cache: Dict[str, PersonalContext] = {}
        print("ContextCache initialized.")

    def get(self, node_id: str) -> PersonalContext | None:
        """Returns a context from the cache or None if not found."""
        return self._cache.get(node_id)

    def set(self, node_id: str, context: PersonalContext):
        """Saves a context to the cache."""
        print(f"Caching context for node {node_id}.")
        self._cache[node_id] = context

    def clear(self):
        """Clears the entire cache."""
        self._cache.clear()