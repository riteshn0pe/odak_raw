"""Cache and rate limiting.

Two distinct jobs share one store:

  cache        avoid re-reading things that rarely change, mainly agent config
  rate limit   bound what one tenant or one visitor can spend

The rate limiter matters more than the cache. A leaked or over-used API key can
drive the chat endpoint, and per-tenant and per-visitor ceilings are what keep
that from becoming free inference on our bill.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Any
from uuid import uuid4

from app.core.errors import RateLimitError
from app.core.logging import get_logger
from app.domain.ports import CachePort

logger = get_logger(__name__)


class LockAcquisitionError(RuntimeError):
    """Raised when a keyed lease cannot be acquired before its wait deadline."""


class MemoryCache:
    """In-process cache for development and single-instance deploys.

    Honest about its limit: entries are per worker, so two workers see different
    state. That is fine for config caching and wrong for rate limiting anything
    that must hold across a fleet — use Redis there.
    """

    def __init__(self) -> None:
        self._values: dict[str, tuple[str, float | None]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._values.get(key)
            if entry is None:
                return None
            value, expires = entry
            if expires is not None and expires < time.time():
                # expire lazily. a sweep would cost more than it saves at this scale
                del self._values[key]
                return None
            return value

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        async with self._lock:
            expires = time.time() + ttl_seconds if ttl_seconds else None
            self._values[key] = (value, expires)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(key, None)

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        async with self._lock:
            entry = self._values.get(key)
            now = time.time()

            if entry is None or (entry[1] is not None and entry[1] < now):
                # the TTL is set on the first increment of a window and not
                # refreshed after, so the window is fixed rather than sliding
                expires = now + ttl_seconds if ttl_seconds else None
                self._values[key] = ("1", expires)
                return 1

            count = int(entry[0]) + 1
            self._values[key] = (str(count), entry[1])
            return count

    async def acquire_lease(self, key: str, owner_token: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")

        async with self._lock:
            entry = self._values.get(key)
            now = time.time()
            if entry is not None and (entry[1] is None or entry[1] >= now):
                return False

            self._values[key] = (owner_token, now + ttl_seconds)
            return True

    async def release_lease(self, key: str, owner_token: str) -> bool:
        async with self._lock:
            entry = self._values.get(key)
            if entry is None or entry[0] != owner_token:
                return False

            del self._values[key]
            return True

    async def close(self) -> None:
        self._values.clear()


class RedisCache:
    """Redis over its REST interface.

    REST rather than a connection pool because the target host scales to zero —
    a pooled TCP client spends its life reconnecting there.
    """

    def __init__(self, url: str, token: str = "", timeout: float = 10.0) -> None:
        import httpx

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers=headers,
        )

    async def _command(self, *parts: Any) -> Any:
        """Run one Redis command.

        Cache and rate-limit failures must never fail the request they were
        protecting, so errors are logged and swallowed by the callers below.
        """
        response = await self._client.post("/", json=[str(part) for part in parts])
        if response.status_code >= 400:
            raise RuntimeError(f"redis {response.status_code}: {response.text[:200]}")
        return response.json().get("result")

    async def get(self, key: str) -> str | None:
        try:
            result = await self._command("GET", key)
        except Exception:
            logger.warning("cache_get_failed", key=key, exc_info=True)
            return None
        return str(result) if result is not None else None

    async def set(self, key: str, value: str, ttl_seconds: int | None = None) -> None:
        try:
            if ttl_seconds:
                await self._command("SET", key, value, "EX", ttl_seconds)
            else:
                await self._command("SET", key, value)
        except Exception:
            logger.warning("cache_set_failed", key=key, exc_info=True)

    async def delete(self, key: str) -> None:
        try:
            await self._command("DEL", key)
        except Exception:
            logger.warning("cache_delete_failed", key=key, exc_info=True)

    async def incr(self, key: str, ttl_seconds: int | None = None) -> int:
        try:
            count = int(await self._command("INCR", key))
            if count == 1 and ttl_seconds:
                # only the first increment sets expiry, so the window does not
                # slide forward on every request
                await self._command("EXPIRE", key, ttl_seconds)
            return count
        except Exception:
            logger.warning("rate_limit_incr_failed", key=key, exc_info=True)
            # fail open. a broken cache should not lock every tenant out
            return 0

    async def acquire_lease(self, key: str, owner_token: str, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")

        try:
            result: object = await self._command(
                "SET", key, owner_token, "NX", "EX", ttl_seconds
            )
            return result == "OK"
        except Exception:
            logger.warning("lease_acquire_failed_open", key=key, exc_info=True)
            return True

    async def release_lease(self, key: str, owner_token: str) -> bool:
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            result: object = await self._command("EVAL", script, 1, key, owner_token)
            return result == 1
        except Exception:
            logger.warning("lease_release_failed_open", key=key, exc_info=True)
            return False

    async def close(self) -> None:
        await self._client.aclose()


class KeyedLock:
    """Short-lived distributed lock backed by an owner-token lease."""

    def __init__(
        self,
        cache: CachePort,
        key: str,
        *,
        ttl_seconds: int = 30,
        wait_timeout: float = 1.0,
        retry_interval: float = 0.05,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("lease TTL must be positive")
        if wait_timeout < 0:
            raise ValueError("lock wait timeout cannot be negative")
        if retry_interval <= 0:
            raise ValueError("lock retry interval must be positive")

        self._cache: CachePort = cache
        self._key: str = key
        self._ttl_seconds: int = ttl_seconds
        self._wait_timeout: float = wait_timeout
        self._retry_interval: float = retry_interval
        self._owner_token: str = uuid4().hex
        self._acquired: bool = False

    @property
    def owner_token(self) -> str:
        return self._owner_token

    async def __aenter__(self) -> KeyedLock:
        deadline = time.monotonic() + self._wait_timeout
        while True:
            if await self._cache.acquire_lease(
                self._key, self._owner_token, self._ttl_seconds
            ):
                self._acquired = True
                return self

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                message = f"lock {self._key!r} not acquired in {self._wait_timeout:g}s"
                raise LockAcquisitionError(message)
            await asyncio.sleep(min(self._retry_interval, remaining))

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._acquired:
            try:
                _ = await asyncio.shield(
                    self._cache.release_lease(self._key, self._owner_token)
                )
            finally:
                self._acquired = False


class RateLimiter:
    """Fixed-window counters over whatever cache is configured.

    Fixed window rather than sliding: it costs one counter per window instead of
    a sorted set per key, and the burst it permits at a boundary is acceptable
    for abuse control. It is not a usage meter — `usage_events` is.
    """

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    async def check(
        self, scope: str, identifier: str, limit: int, window_seconds: int = 60
    ) -> None:
        if limit <= 0:
            return

        # the window is in the key, so a new window is a new counter and no
        # reset job is needed
        window = int(time.time() // window_seconds)
        key = f"rl:{scope}:{identifier}:{window}"

        count = await self._cache.incr(key, window_seconds)

        # 0 means the cache failed and reported it. failing open is deliberate:
        # a cache outage should degrade abuse control, not availability
        if count and count > limit:
            retry_after = window_seconds - int(time.time() % window_seconds)
            logger.info("rate_limited", scope=scope, identifier=identifier, count=count)
            raise RateLimitError(
                f"rate limit exceeded for {scope}", retry_after=max(retry_after, 1)
            )


def build_cache(settings: Any) -> Any:
    if settings.CACHE_BACKEND == "redis" and settings.REDIS_URL:
        return RedisCache(settings.REDIS_URL, settings.REDIS_TOKEN)
    return MemoryCache()
