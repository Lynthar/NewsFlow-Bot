"""
Cache service abstraction.

Provides both in-memory and Redis caching backends.
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class CacheBackend(ABC):
    """Abstract base class for cache backends."""

    @abstractmethod
    async def get(self, key: str) -> str | None:
        """Get a value from cache."""
        pass

    @abstractmethod
    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        """
        Set a value in cache.

        Args:
            key: Cache key.
            value: Value to cache.
            ttl: Time-to-live in seconds. None for no expiration.

        Returns:
            True if successful.
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete a value from cache."""
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if a key exists in cache."""
        pass

    @abstractmethod
    async def clear(self) -> None:
        """Clear all cached values."""
        pass


class MemoryCache(CacheBackend):
    """
    In-memory LRU cache backend.

    Suitable for single-instance deployments.
    """

    def __init__(self, max_size: int = 10000) -> None:
        self.max_size = max_size
        self._cache: OrderedDict[str, tuple[str, float | None]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            if key not in self._cache:
                return None

            value, expires_at = self._cache[key]

            # Check expiration
            if expires_at and time.time() > expires_at:
                del self._cache[key]
                return None

            # Move to end (LRU)
            self._cache.move_to_end(key)
            return value

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        async with self._lock:
            expires_at = time.time() + ttl if ttl else None

            # Remove if exists (to update order)
            if key in self._cache:
                del self._cache[key]

            # Evict oldest if at capacity
            while len(self._cache) >= self.max_size:
                self._cache.popitem(last=False)

            self._cache[key] = (value, expires_at)
            return True

    async def delete(self, key: str) -> bool:
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def exists(self, key: str) -> bool:
        async with self._lock:
            if key not in self._cache:
                return False

            _, expires_at = self._cache[key]
            if expires_at and time.time() > expires_at:
                del self._cache[key]
                return False

            return True

    async def clear(self) -> None:
        async with self._lock:
            self._cache.clear()

    def size(self) -> int:
        """Return current cache size."""
        return len(self._cache)


class RedisCache(CacheBackend):
    """
    Redis cache backend.

    Suitable for multi-instance deployments.
    """

    def __init__(self, redis_url: str) -> None:
        self.redis_url = redis_url
        self._client: Any = None

    async def _get_client(self) -> Any:
        """Lazy initialization of Redis client."""
        if self._client is None:
            try:
                import redis.asyncio as redis

                self._client = redis.from_url(
                    self.redis_url,
                    encoding="utf-8",
                    decode_responses=True,
                )
            except ImportError:
                raise ImportError(
                    "redis package is required for Redis caching. "
                    "Install it with: pip install redis[hiredis]"
                )
        return self._client

    async def get(self, key: str) -> str | None:
        try:
            client = await self._get_client()
            return await client.get(key)
        except Exception as e:
            logger.exception(f"Redis get error: {e}")
            return None

    async def set(self, key: str, value: str, ttl: int | None = None) -> bool:
        try:
            client = await self._get_client()
            if ttl:
                await client.setex(key, ttl, value)
            else:
                await client.set(key, value)
            return True
        except Exception as e:
            logger.exception(f"Redis set error: {e}")
            return False

    async def delete(self, key: str) -> bool:
        try:
            client = await self._get_client()
            result = await client.delete(key)
            return result > 0
        except Exception as e:
            logger.exception(f"Redis delete error: {e}")
            return False

    async def exists(self, key: str) -> bool:
        try:
            client = await self._get_client()
            result = await client.exists(key)
            return result > 0
        except Exception as e:
            logger.exception(f"Redis exists error: {e}")
            return False

    async def clear(self) -> None:
        try:
            client = await self._get_client()
            await client.flushdb()
        except Exception as e:
            logger.exception(f"Redis clear error: {e}")

    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None


# Global cache instance
_cache: CacheBackend | None = None


def get_cache() -> CacheBackend | None:
    """Get the global cache instance."""
    return _cache


def init_cache(backend: str = "memory", **kwargs: Any) -> CacheBackend:
    """
    Initialize the global cache.

    Args:
        backend: "memory" or "redis"
        **kwargs: Backend-specific arguments
            - memory: max_size (int)
            - redis: redis_url (str)

    Returns:
        Initialized cache backend.
    """
    global _cache

    if backend == "redis":
        redis_url = kwargs.get("redis_url", "redis://localhost:6379/0")
        _cache = RedisCache(redis_url)
    else:
        max_size = kwargs.get("max_size", 10000)
        _cache = MemoryCache(max_size)

    logger.info(f"Initialized {backend} cache backend")
    return _cache
