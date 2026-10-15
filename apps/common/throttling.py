"""Throttles for sensitive endpoints, built on DRF's cache-backed SimpleRateThrottle.

Client identity comes from DRF's get_ident(): REMOTE_ADDR by default, or the right-most
trusted X-Forwarded-For entry when NUM_PROXIES is set (Nginx = 1, configured in Phase 2).
The left-most X-Forwarded-For value is never used because clients can forge it.
"""

import hashlib

from rest_framework.throttling import SimpleRateThrottle


class _IPScopedThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class LoginIPThrottle(_IPScopedThrottle):
    """Limits login attempts per client IP (rate: THROTTLE_LOGIN)."""

    scope = "login"


class RegisterThrottle(_IPScopedThrottle):
    """Limits sign-ups per client IP (rate: THROTTLE_REGISTER)."""

    scope = "register"


class LoginAccountThrottle(SimpleRateThrottle):
    """Limits login attempts per target account, whatever IP they come from.

    Slows credential stuffing that rotates IPs. The key is a SHA-256 of the normalized email,
    so the cache never stores addresses. Trade-off: someone can briefly slow logins to a
    victim's account by spamming it (5 attempts per minute), which is far cheaper than a breach.
    """

    scope = "login_account"
    rate = "5/min"

    def get_cache_key(self, request, view):
        data = request.data
        if not hasattr(data, "get"):
            return None
        email = str(data.get("email", "")).strip().lower()[:254]
        if not email:
            return None  # nothing to key on; the per-IP throttle still applies
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}
