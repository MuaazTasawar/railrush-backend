# ADR 0002: Virtual waiting room: a leaky-bucket queue per run, with signed passes

Status: Accepted (Phase 5, October 2026); admission rate re-examined in Phase 8

Note on sources: the Phase 5 report records tests and bugs, not alternatives, so none are listed. This ADR is
written from `apps/waitroom/` and the Phase 8 measurements in `docs/CAPACITY.md`.

## Context

Everyone opens the app when booking opens. The system has to admit people at a rate it can serve, in the order they
arrived, without a background worker to run and without clients polling constantly.

## Decision

Each run has its own queue, with all state in Redis (default prefix `wr`):

- `wr:{run}:issued`: the ticket counter.
- `wr:{run}:next`: the next free admission slot, a timestamp.
- `wr:{run}:u:<user>`: the user's record, "ticket:admit_at".

Joining is one Lua script, so it is atomic:

- A user who already has a record gets the same ticket back, so joining twice is harmless.
- When `WAITROOM_MAX_QUEUE` (200,000) tickets have been issued, the join is refused and the API answers 503.
- Otherwise the user's slot is the latest of the next free slot, now and the time booking opens. The next free slot
  then moves forward by 1 / `WAITROOM_ADMIT_PER_SECOND`.

Admissions are therefore evenly spaced however bursty the joins are, an idle queue never stockpiles free
admissions, and a burst after idleness is still paced (all covered by Phase 5 tests). Joining opens
`WAITROOM_PREQUEUE_SECONDS` (900) before booking opens; early joiners queue from the opening time in join order.

The client contract: the join answer carries `admit_at`. The client sleeps until then and asks for its pass. No
worker and almost no polling are needed.

A pass is a token signed with Django's signing module under its own salt. It carries the user id, the run id and an
expiry, and it is valid for `WAITROOM_PASS_TTL_SECONDS` (600) after the user's own admission time. Verifying it needs
no database or Redis lookup. It is bound to one user and one run, an access or refresh token can never pass as a
pass, and it is sent in the `X-Waitroom-Pass` header. Placing a hold requires it.

The public status endpoint shows queue-wide numbers only, so nginx may cache it for 1 second. The run schedule is
cached in the Django cache for 30 seconds (15 seconds for an unknown run).

`WAITROOM_ADMIT_PER_SECOND` is per run (default 50).

## Consequences

- The order is join order, not a lottery. Someone who joins early in the pre-queue is ahead of everyone who joins later.
- Slots are never reclaimed. A person who is admitted and never acts still uses their slot.
- A person queueing for two trains holds two tickets, one per run.
- The rate is read from the environment when the process starts, so changing it needs a web restart, and it only
  affects people who join afterwards: tickets already issued keep their admission time. Making it adjustable
  without a restart is on the open list.
- The default of 50 per run is about 8 times what the Phase 8 test rig sustained cleanly for one hot train. Size it
  from `docs/CAPACITY.md`.
- Time comes from the web process (`time.time()`) and is passed into the script, not taken from Redis. With several
  web hosts, their clocks must be kept in sync.
- Slots are stored with 6 decimals, so a stored slot can sit up to half a microsecond after "now". `TIME_EPSILON`
  makes that person count as admitted; the Phase 5 test run found this as a real bug.