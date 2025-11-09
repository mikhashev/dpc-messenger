"""
Token blacklist for logout and token revocation.

This module provides an in-memory blacklist for JWT tokens.
For production at scale, consider using Redis for distributed blacklist.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Set, Dict

logger = logging.getLogger(__name__)


class TokenBlacklist:
    """
    In-memory token blacklist for logout functionality.
    
    Tokens are blacklisted when users logout. The blacklist is cleaned
    periodically to remove expired entries.
    
    For production with multiple Hub instances, replace with Redis:
    - Use Redis SET with TTL for each blacklisted token
    - TTL = token expiration time
    - Distributed across all Hub instances
    """
    
    def __init__(self, cleanup_interval: int = 3600):
        """
        Initialize the blacklist.
        
        Args:
            cleanup_interval: Seconds between cleanup runs (default: 1 hour)
        """
        self._blacklist: Set[str] = set()
        self._blacklist_timestamps: Dict[str, datetime] = {}
        self._cleanup_interval = cleanup_interval
        self._cleanup_task = None
        logger.info("TokenBlacklist initialized")
    
    def start(self):
        """Start background cleanup task"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("TokenBlacklist cleanup task started")
    
    async def stop(self):
        """Stop background cleanup task"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
            logger.info("TokenBlacklist cleanup task stopped")
    
    async def _cleanup_loop(self):
        """
        Background task to remove expired tokens from blacklist.
        Runs periodically to prevent memory growth.
        """
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in blacklist cleanup: {e}")
    
    async def _cleanup_expired(self):
        """Remove tokens that have been blacklisted for more than 1 hour"""
        now = datetime.now(timezone.utc)
        expired_threshold = now - timedelta(hours=1)
        
        expired_tokens = [
            token for token, timestamp in self._blacklist_timestamps.items()
            if timestamp < expired_threshold
        ]
        
        for token in expired_tokens:
            self._blacklist.discard(token)
            del self._blacklist_timestamps[token]
        
        if expired_tokens:
            logger.info(f"Cleaned up {len(expired_tokens)} expired tokens from blacklist")
    
    def add(self, token: str):
        """
        Add a token to the blacklist (logout).
        
        Args:
            token: JWT token string to blacklist
        """
        self._blacklist.add(token)
        self._blacklist_timestamps[token] = datetime.now(timezone.utc)
        logger.info(f"Token added to blacklist (total: {len(self._blacklist)})")
    
    def is_blacklisted(self, token: str) -> bool:
        """
        Check if a token is blacklisted.
        
        Args:
            token: JWT token string to check
        
        Returns:
            True if token is blacklisted (user logged out)
        """
        return token in self._blacklist
    
    def remove(self, token: str):
        """
        Remove a token from the blacklist (for testing/admin).
        
        Args:
            token: JWT token string to remove
        """
        self._blacklist.discard(token)
        self._blacklist_timestamps.pop(token, None)
        logger.info(f"Token removed from blacklist")
    
    def clear(self):
        """Clear all tokens from blacklist (for testing/admin)"""
        count = len(self._blacklist)
        self._blacklist.clear()
        self._blacklist_timestamps.clear()
        logger.info(f"Blacklist cleared ({count} tokens removed)")
    
    def size(self) -> int:
        """Get current blacklist size"""
        return len(self._blacklist)


# Global instance
_blacklist_instance = None


def get_blacklist() -> TokenBlacklist:
    """Get the global blacklist instance"""
    global _blacklist_instance
    if _blacklist_instance is None:
        _blacklist_instance = TokenBlacklist()
    return _blacklist_instance


def start_blacklist():
    """Start the global blacklist cleanup task"""
    blacklist = get_blacklist()
    blacklist.start()


async def stop_blacklist():
    """Stop the global blacklist cleanup task"""
    blacklist = get_blacklist()
    await blacklist.stop()


# For production with Redis:
"""
import redis.asyncio as redis

class RedisTokenBlacklist:
    def __init__(self, redis_url: str):
        self.redis = redis.from_url(redis_url)
    
    async def add(self, token: str, ttl: int = 3600):
        # Add with TTL matching token expiration
        await self.redis.setex(f"blacklist:{token}", ttl, "1")
    
    async def is_blacklisted(self, token: str) -> bool:
        result = await self.redis.exists(f"blacklist:{token}")
        return result > 0
    
    async def close(self):
        await self.redis.close()
"""