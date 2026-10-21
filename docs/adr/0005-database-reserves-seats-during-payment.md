# ADR 0005: The database reserves seats during payment

Status: Accepted (Phase 7, 21 October 2026)

## Context

Phase 6 holds a seat in Redis for 300 seconds (ADR 0001). Paying takes longer than that, so a buyer needs
the seats kept for about 10 minutes after they commit to buying. Two ways to do it:

1. Extend the Redis hold while the buyer pays.
2. Reserve the seats in PostgreSQL by creating the booking, and let the Redis holds go.

## Decision

Option 2. Creating a booking inserts a `Booking` and one `BookingSeat` row per seat in one transaction.
A partial unique index on `(run, seat_label)` where `active` is true makes a second active row for the
same seat impossible. The user's Redis holds are released after the transaction commits.
`BOOKING_PAYMENT_SECONDS` (600) sets the deadline. Celery beat runs `expire_pending_bookings` every
30 seconds and marks overdue pending bookings expired, which sets their seats inactive.

A payment that arrives after expiry tries to re-activate the seats inside a savepoint. If another buyer
has taken them, the booking becomes `refund_required` for manual refund. A payment that arrives for a
cancelled booking is also `refund_required`.

## Consequences

- A sold seat is protected by PostgreSQL, so it stays protected if Redis loses its data.
- No new Lua script is needed, and the buyer's hold slots are free again as soon as they book.
- The hold endpoint reads sold seats from the database on every request (one indexed query). Measure it in
  Phase 8. If it matters, keep a Redis set of sold seats, updated after commit and rebuilt from the database.
- A booking can stay up to about 30 seconds past its deadline until beat runs. The late-payment path covers the gap.
- Refunds are manual until a refund flow exists.
