# ADR 0006: Rate limiting with one-call Redis fixed-window throttles

Status: Accepted (Phase 8, October 2026)

## Context

DRF's stock throttles keep a list of request times per key in Django's cache. That costs two cache calls per
throttle (a get and a set), and Django's Redis cache backend builds a new redis-py client object for every call.
Every API request passes through up to three default throttle classes, and the web tier is the CPU-bound part of the
system (about 70% of the CPU cost per admitted user, see `docs/CAPACITY.md`).

## Decision

The default throttle classes in `config/settings/base.py` are `FastAnonRateThrottle`, `FastUserRateThrottle` and
`FastScopedRateThrottle` (`apps/common/fast_throttle.py`). Each request does one atomic Lua call on the app's shared
Redis client: INCR a counter, and set its expiry when the key is new. The key prefix is `thr:`. The rates are the
same `THROTTLE_*` settings as before.

- `FAST_THROTTLES=false` switches every class back to DRF's own behaviour, for A/B tests and as an emergency rollback.
- When the default cache is not Redis (local runs on the in-process cache), the classes behave exactly like DRF's.
- A Redis error is reported as `DependencyUnavailable`, which the API answers with 503 and a retry hint.
- The account-level login, register and refresh throttles in `apps/common/throttling.py` still use DRF's
  `SimpleRateThrottle` and were not converted.

## Evidence

Figures recorded in `docs/CAPACITY.md` (Phase 8, on one slow and noisy laptop):

- In isolation, a throttle check took 0.8 ms at the median instead of 6.8 ms.
- In the profile of a hold request, the throttle share of the request fell from 43% to 17%.
- An end-to-end A/B comparison of whole requests could not show a difference, because machine speed varied more than
  the effect. The gain is therefore measured per throttle check, not demonstrated for the full journey.

## Consequences

- A fixed window lets a client send up to twice the limit around a window boundary.
- Each key holds one small counter instead of a list of timestamps, which uses far less Redis memory for 35,000 users.
- Throttle counters live in the same Redis as seat holds and the waiting room (Redis uses `noeviction`). Counter keys
  expire by themselves after one window.
- A Redis outage now answers 503 on throttled endpoints instead of failing with an unhandled error.
- Revisit if a sliding window or a per-endpoint burst allowance is required.