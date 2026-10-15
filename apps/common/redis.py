"""Shared Redis client for seat holds, the waiting room and health checks.

One connection pool per process, created on first use. Timeouts are short on purpose:
a slow Redis must fail fast instead of pinning application threads.
"""

from functools import lru_cache

import redis
from django.conf import settings

SOCKET_TIMEOUT_SECONDS = 1.0
MAX_CONNECTIONS = 100


@lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        max_connections=MAX_CONNECTIONS,
        health_check_interval=30,
    )


def reset_redis_client() -> None:
    """Drop the cached client (used by tests and after settings changes)."""
    get_redis.cache_clear()
