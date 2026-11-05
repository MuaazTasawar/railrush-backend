# ADR 0001: Seat holds live in Redis, behind atomic Lua scripts

Status: Accepted (Phase 6, October 2026)

Note on sources: the Phase 6 report does not record a comparison with other designs, so this ADR lists no
alternatives. It is written from the code (`apps/bookings/holds.py`), `deploy/redis/redis.conf` and the later phases.
If the options that were weighed at the time are recovered, add them here.

## Context

During a booking window many people ask for the same seats within seconds. A hold is a short, exclusive claim on a
seat while its holder pays. The code needs these properties:

- Two users can never hold the same seat.
- A request for several seats gets all of them or none.
- One user can never exceed `HOLD_MAX_SEATS_PER_USER` (4), however many requests race.
- A hold ends by itself after `HOLD_SECONDS` (300).

## Decision

Hold state lives in Redis, not in PostgreSQL: three keys per run. The braces are a hash tag that keeps one run's
keys together.

| Key | Type | Content |
|---|---|---|
| `hold:{run}:z` | sorted set | seat -> expiry time |
| `hold:{run}:o` | hash | seat -> id of the holder |
| `hold:{run}:u:<user>` | sorted set | that user's seats -> expiry time |

Holding and releasing are one Lua script each, so each runs as a single atomic step:

- Hold first purges the expired entries of the run and of the user. Then it checks every requested seat. If another
  user holds any of them, it returns those seats and changes nothing. If the user would pass the per-user limit, it
  returns the number they already hold and changes nothing. Otherwise it takes all the seats.
- Asking again for a seat you already hold is harmless and does not extend its expiry.
- Release frees only seats the caller holds (all of them when none are named) and reports those still live.
- A seat counts as held only while its expiry is in the future, so an expired hold that nobody has purged yet is
  already treated as gone by reads. Each hold also sets every key to expire `HOLD_SECONDS` plus 60 seconds later,
  so the keys of a finished run disappear.
- The caller passes the current time into the script (`waitroom.now()`), so tests control the clock in one place.

Redis is configured so this state is never dropped silently: `maxmemory-policy noeviction`, and an append-only file
synced every second (a crash can lose about one second of holds). PostgreSQL stays the source of truth for
bookings. A booking is made only for seats the user still holds (`verify_holds`), it reserves them in the database
(ADR 0005), and the Redis holds are released after that commit.

## Consequences

- Holds do not write to PostgreSQL. Since Phase 7 each hold request still reads the sold and reserved seats of the
  run from PostgreSQL (see `docs/CAPACITY.md`).
- If Redis loses data, buyers can lose holds (a seat may be taken before they book), but a seat cannot be sold
  twice: the partial unique index in PostgreSQL is the last guard (ADR 0005).
- When Redis is full, writes fail instead of evicting. That is deliberate (see the comment in `redis.conf`).
- Hold and release requests share one throttle bucket (`THROTTLE_HOLD`, 30 per minute per user). The Phase 6 smoke
  test saw the 429 at request 28 for that reason.
- The public seat map is cached by nginx for 1 second and can be up to about 2 seconds old. The hold script, not
  the map, decides who gets a seat.
- The script uses the web server's clock. With several web hosts, their clocks must be kept in sync.