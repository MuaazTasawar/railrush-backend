# ADR 0003: Process model: threaded WSGI, not ASGI

Status: Accepted (Phase 8, 24 October 2026)

## Context

`deploy/gunicorn.conf.py` can run gunicorn threads (`gthread`, `config.wsgi`) or Uvicorn workers (`config.asgi`). All
our views are synchronous: they use the Django ORM and redis-py, and there are no async views.

## Decision

Run gunicorn `gthread` with 3 workers of 16 threads per web container (`WEB_CONCURRENCY=3`, `GUNICORN_THREADS=16`) and
scale by adding containers. The ASGI settings stay in the config for a future with async views.

## Evidence

400 users, 2 runs, 6 admissions per second per run, one run per cell, in this order, on a laptop whose speed varied:

| Cell | Requests/s | Confirmed | Gave up in the queue | Web CPU per request | Machine benchmark before to after |
|---|---|---|---|---|---|
| gthread, 4 workers | 18.9 | 215 | none | 58 ms | 2.46 s to 0.50 s |
| ASGI (Uvicorn), 3 workers | 18.8 | 183 | 109 | 73 ms | 1.57 s to 0.97 s |
| gthread, 2 workers | 33.9 | 329 | none | 30 ms | 0.79 s to 0.45 s |
| gthread, 3 workers | 55.6 | 329 | none | 25 ms | 0.95 s to 1.48 s |

Only large gaps count, because the machine's speed changed between cells. ASGI had the highest CPU cost per request and
was the only cell where users gave up waiting. Three workers beat two even though the two-worker cell ran on a faster
machine. The four-worker cell started while the machine was at its slowest, so it says little; four processes on two
physical cores should contend, and 3 stays the default.

## Why ASGI loses here (likely)

Under ASGI, Django runs synchronous views through a thread-sensitive executor, which runs them one at a time per
process. Three ASGI workers then behave like three concurrent requests, against 48 threads under gthread. This is the
likely explanation and was not tested separately.

## Consequences

- Capacity per web container is bounded by about 3 cores of Python work; scale web horizontally first.
- Revisit if the hot paths become async (an async Redis client and async views).
