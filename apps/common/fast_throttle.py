"""Fast DRF throttles: one atomic Redis call per request instead of a Django-cache get plus set.

DRF's default throttles keep a list of request times in the Django cache, which costs two cache calls per
throttle, and Django's Redis cache backend builds a new redis-py client object for every call. These classes keep a
fixed-window counter instead (INCR plus PEXPIRE in a single Lua call on the app's shared Redis client).

Trade-offs: a client can send up to twice the limit around a window boundary, and each key holds one small
counter instead of a list of timestamps (far less Redis memory for 35,000 users). When the default cache is not
Redis (local runs on the in-process cache) these classes behave exactly like DRF's own.
"""

from django.conf import settings
from redis.exceptions import RedisError
from rest_framework import throttling

from apps.common.redis import get_redis

KEY_PREFIX = "thr:"

HIT_LUA = """
local count = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if count == 1 or ttl < 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
return {count, ttl}
"""


def _cache_is_redis():
    """The fast path needs a Redis cache. FAST_THROTTLES=false switches it off (A/B tests and emergency rollback)."""
    if not getattr(settings, "FAST_THROTTLES", True):
        return False
    return settings.CACHES["default"]["BACKEND"].endswith("RedisCache")


def hit(key, window_seconds):
    """Count one request in the key's current window. Returns (count so far, milliseconds until the window ends)."""
    try:
        script = get_redis().register_script(HIT_LUA)
        count, ttl = script(keys=[KEY_PREFIX + key], args=[int(window_seconds * 1000)])
    except RedisError as exc:
        # Imported here on purpose: apps.common.exceptions imports DRF, whose settings import this module.
        from apps.common.exceptions import DependencyUnavailable

        raise DependencyUnavailable("Rate limiting is temporarily unavailable. Please retry shortly.") from exc
    return int(count), int(ttl)


class FixedWindowThrottle(throttling.SimpleRateThrottle):
    def allow_request(self, request, view):
        if not _cache_is_redis():
            return super().allow_request(request, view)
        if self.rate is None:
            return True
        self.key = self.get_cache_key(request, view)
        if self.key is None:
            return True
        count, self._retry_ms = hit(self.key, self.duration)
        return count <= self.num_requests

    def wait(self):
        if not _cache_is_redis():
            return super().wait()
        return max(getattr(self, "_retry_ms", 1000), 1) / 1000.0


class FastAnonRateThrottle(FixedWindowThrottle):
    scope = "anon"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class FastUserRateThrottle(FixedWindowThrottle):
    scope = "user"

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class FastScopedRateThrottle(FixedWindowThrottle):
    scope_attr = "throttle_scope"

    def __init__(self):
        # The rate depends on the view, so it is resolved per request (as in DRF's ScopedRateThrottle).
        pass

    def allow_request(self, request, view):
        self.scope = getattr(view, self.scope_attr, None)
        if not self.scope:
            return True
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        ident = request.user.pk if request.user and request.user.is_authenticated else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
