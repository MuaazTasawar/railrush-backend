# ADR 0008: No uniform in-flight request cap at nginx

Status: Rejected (Phase 8, October 2026)

## Context

When admissions exceed what the web tier can serve, requests queue behind the gunicorn threads (3 workers x 16
threads) and latency climbs. At 24 admissions per second per run the sweeps showed p95 hold and book times of
21 to 23 s in a slow machine state (`docs/CAPACITY.md`). The idea: cap concurrent requests at nginx with a
`limit_conn` zone keyed on the server name, sized at about 2 x workers x threads (96), and answer 429 at once
instead of letting requests queue.

## Experiment

Four alternating runs with and without the cap (96), at 24 admissions per second per run, on the load-test nginx
configuration only:

- Completed purchases fell from about 320 to about 165.
- 1,400 to 2,000 requests per run were refused, including booking and payment steps in the middle of a purchase.
- The uncapped runs saw no timeouts.
- k6 counts the instant 429 answers as fast requests, so request-latency percentiles look better with the cap and
  are misleading. The right measure is completed purchases.

The effect is large (about half), so it is not explained by the machine's speed swings.

## Decision

Not adopted. The production `deploy/nginx/nginx.conf` has no in-flight zone. The per-IP limits (`limit_req`,
`limit_conn connperip 300`) stay. The cap can still be injected into the generated load-test nginx file only, with
`loadtest/capab.ps1` and `LT_INFLIGHT`. Load is controlled upstream instead: by the waiting-room admission rate,
which is set per run and sized from the formula in `docs/CAPACITY.md`.

## Consequences

- There is no global load shedding. Overload at the web tier shows up as latency, not as refusals. The limits that
  remain are the admission rate, the nginx per-IP limits, the app throttles, and the gunicorn and nginx timeouts
  (30 s).
- If a cap is tried again: shed only at the entry (join and hold), never in the middle of a purchase. Never shed the
  payment webhook (the load-test configuration already exempts it, because a refused webhook delays a paid booking).
  Judge the result by completed purchases, not by request latency. Entry-only shedding has not been tested.