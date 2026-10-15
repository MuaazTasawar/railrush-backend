"""Liveness and readiness probes.

/api/health/live/   The process is up. Touches nothing else, so it cannot flap when a dependency is slow.
/api/health/ready/  This instance can serve traffic. Checks the database and Redis.

Responses never include hostnames, versions or error text. Readiness results are memoized for
a couple of seconds, so the endpoint cannot be used to hammer the database or Redis.
"""

import logging
import threading
import time

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .redis import get_redis

logger = logging.getLogger(__name__)

READINESS_TTL_SECONDS = 2.0

_lock = threading.Lock()
_cached = {"at": 0.0, "result": None}


def check_database() -> bool:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception:
        logger.warning("Readiness: database check failed", exc_info=True)
        return False


def check_redis() -> bool:
    try:
        return bool(get_redis().ping())
    except Exception:
        logger.warning("Readiness: redis check failed", exc_info=True)
        return False


def run_readiness_checks() -> dict:
    checks = {"database": check_database(), "redis": check_redis()}
    return {
        "ok": all(checks.values()),
        "checks": {name: "ok" if passed else "fail" for name, passed in checks.items()},
    }


def get_readiness() -> dict:
    """Return the readiness result, re-running the checks at most once per TTL (stampede-safe)."""
    with _lock:
        result = _cached["result"]
        if result is not None and time.monotonic() - _cached["at"] < READINESS_TTL_SECONDS:
            return result
        result = run_readiness_checks()
        _cached["result"] = result
        _cached["at"] = time.monotonic()
        return result


def reset_readiness_cache() -> None:
    with _lock:
        _cached["result"] = None
        _cached["at"] = 0.0


@require_safe
@never_cache
def live(request):
    return JsonResponse({"status": "ok"})


@require_safe
@never_cache
def ready(request):
    result = get_readiness()
    body = {"status": "ok" if result["ok"] else "unavailable", "checks": result["checks"]}
    return JsonResponse(body, status=200 if result["ok"] else 503)
