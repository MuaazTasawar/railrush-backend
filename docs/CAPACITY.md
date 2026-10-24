

## Phase 7 note: sold-seat lookups

Every hold request now reads the sold and reserved seats of the run from PostgreSQL (one query on
`bookingseat_run_active_idx`). That is acceptable at the current rates (30 holds per minute per user, admission
paced by the waiting room), but it is the first thing to measure in the Phase 8 load test. If it shows up, keep a
Redis set of sold seats per run, update it after the booking commits, and rebuild it from the database when it is missing.

## Phase 8: measured capacity and sizing

Measured 21 to 24 October 2026 with the k6 harness in `loadtest/` (see `loadtest/README.md`). Read "The rig" first: the
absolute numbers belong to one slow, noisy laptop.

### The rig

- One Windows laptop (Intel i7-7500U: 2 cores, 4 threads, a 15 W part). Docker Desktop gets 4 vCPUs and about 5.8 GiB,
  and the whole stack and the load generator share them.
- Its speed is very uneven. The same 2,000,000-iteration Python loop (`loadtest/bench_cpu.py`) took 0.4 to 2.6 s within
  one afternoon, plugged in, on the High performance power plan. Every result row records that benchmark before and
  after the run. Latency and throughput move by 2 to 5 times with it, and CPU cost per user by about 2 times. Treat the
  figures as pessimistic, and compare only runs from the same session.
- The journey: join the queue, sleep until `admit_at`, hold 1 or 2 seats, book, pay, signed webhook, read the booking.
  10% abandon before paying and 5% pay unsuccessfully. After every run the data invariants are checked: no seat sold
  twice, every confirmed booking has exactly one succeeded payment of the same amount, and no active seat row belongs
  to a dead booking. They held in all 15 full-journey runs (about 4,300 purchases).

### What serving one user costs

Core-seconds per user, from the containers' own CPU counters (400 users, about 7.2 requests per user):

| | Fast machine state (benchmark 0.5 s) | Middle (1.2 s) | Slow (2.2 s) |
|---|---|---|---|
| Web | 0.14 | 0.18 | 0.36 |
| PostgreSQL | 0.025 | 0.030 | 0.086 |
| Redis | 0.010 | 0.014 | 0.032 |
| PgBouncer | 0.012 | 0.016 | 0.044 |
| nginx | 0.006 | 0.008 | 0.018 |
| **Total** | **0.19** | **0.24** | **0.54** |

Cost scales with CPU speed: about **0.3 core-seconds per user for each second of the benchmark** (range 0.2 to 0.5
across the runs). Web is about 70% of the total, so web scales first; PostgreSQL and Redis stay small.

A request costs the web tier 24 to 58 ms of CPU on this rig under load, against about 4 ms for the same authenticated
request measured inside Django without gunicorn. That gap is not explained yet (suspects: 16 threads contending for the
GIL, per-request logging, the Docker VM). Profile it under gunicorn load on real hardware before trusting the web
figure to the last decimal.

### The waiting-room rate

`WAITROOM_ADMIT_PER_SECOND` is **per run**: each run has its own queue, so the two-run sweeps below admitted twice the
configured rate in total. Results of 400-user bursts spread over 2 runs:

| Rate per run | Users/s in total | p95 hold / book | Throughput | Verdict |
|---|---|---|---|---|
| 3 | 6 | 0.9 / 1.5 s | 35 req/s | clean |
| 6 | 12 | 3.8 / 5.4 s | 60 req/s | saturated |
| 12 | 24 | 8.6 / 9.3 s | 55 req/s | saturated |
| 24 | 48 | 21 / 23 s in a slow machine state, 4 to 12 s in a fast one | 30 req/s slow, 68 to 84 fast | overloaded, no timeouts seen when fast |

These are bursts of 400 users lasting 36 to 96 seconds, not hour-long steady loads. A steady-state soak on
production-like hardware is still to do.

The default of 50 per run is about 8 times what this rig sustains cleanly for one hot train (6 users/s). It is a setting
for larger hardware: size it as below.

### Sizing formula

    cores needed (all services together) = users x K x B / T
    K = 0.3 core-seconds per user per benchmark-second (use 0.5 to be careful)
    B = benchmark seconds on the target machine's vCPU (run loadtest/bench_cpu.py there; do not guess)
    T = seconds over which admissions are spread (users / total admission rate)

    total admission rate to set = usable cores x 0.6 / (K x B)

On the rig (2 usable cores, B about 1.0) that gives 4 users/s (2.4 with K = 0.5); it sustained 6 cleanly. The 0.6 is a
rule-of-thumb headroom for bursts and failures, not a measurement. Split the total over the runs people queue for.

Cores needed for 35,000 users, from K = 0.3 to K = 0.5 (add at least 2 times headroom):

| B on the target vCPU | 15 min (39 users/s) | 30 min (19 users/s) | 60 min (10 users/s) |
|---|---|---|---|
| 1.0 s (like this rig) | 11.7 to 19.4 | 5.8 to 9.7 | 2.9 to 4.9 |
| 0.5 s | 5.8 to 9.7 | 2.9 to 4.9 | 1.5 to 2.4 |
| 0.25 s | 2.9 to 4.9 | 1.5 to 2.4 | 0.7 to 1.2 |
| 0.15 s | 1.8 to 2.9 | 0.9 to 1.5 | 0.4 to 0.7 |

### Adopted

- **Fast Redis throttles** (`apps/common/fast_throttle.py`): one atomic Redis call per throttle instead of DRF's cache
  get plus set. In isolation 0.8 ms against 6.8 ms at the median, and the throttle share of a hold request fell from
  43% to 17% in the profile; the whole-request A/B was drowned in machine noise. `FAST_THROTTLES=false` returns to
  DRF's throttles. Trade-offs: a fixed window can let through up to twice the limit around a window boundary, and a
  Redis outage now answers 503 instead of an unhandled error.
- **Threaded WSGI, 3 workers of 16 threads** (see the process-model ADR).
- **The client contract**: sleep until `admit_at` instead of polling `me` (the join answer already carries it).

### Tried and not adopted

- **ASGI workers**: slower and users gave up waiting (see the ADR).
- **A uniform nginx in-flight cap (96)**, tested with 4 alternating runs at 24 per run: completed purchases fell from
  about 320 to about 165 because 1,400 to 2,000 requests per run were refused, including booking and payment steps. The
  uncapped runs saw no timeouts. If tried again, shed only at the entry (join and hold), never mid-purchase, and
  judge it by completed purchases, not request latency (k6 counts the instant 429s). `loadtest/capab.ps1` with
  `LT_INFLIGHT` injects a cap into the load-test nginx file only.

### Not done yet

- A soak of at least 30 minutes at the chosen admission rate on production-like hardware.
- Profiling under gunicorn load (the 4 ms versus 24 ms gap).
- Making the admission rate adjustable without restarting web (for example from a Redis key).
- Dependency pins: `requirements/base.txt` uses version ranges, so a rebuild can change versions (it did on 21
  October). A lock file with hashes would fix that.
