# dpc-client/core/dpc_client_core/context_cache.py

import logging
from typing import Dict

# We need to import this to use it as a type hint
from dpc_protocol.pcm_core import PersonalContext

logger = logging.getLogger(__name__)

class ContextCache:
    """
    A simple in-memory cache for storing personal contexts received from peers.
    """
    def __init__(self):
        self._cache: Dict[str, PersonalContext] = {}
        logger.debug("ContextCache initialized")

    def get(self, node_id: str) -> PersonalContext | None:
        """Returns a context from the cache or None if not found."""
        return self._cache.get(node_id)

    def set(self, node_id: str, context: PersonalContext):
        """Saves a context to the cache."""
        logger.debug("Caching context for node %s", node_id)
        self._cache[node_id] = context

    def clear(self):
        """Clears the entire cache."""
        self._cache.clear()