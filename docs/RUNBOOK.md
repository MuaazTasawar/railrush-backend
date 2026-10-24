

## Bookings, payments and expiry (Phase 7)

### After rebuilding or recreating `web`

nginx keeps the address of `web` from when it started. After `docker compose up -d --build`, restart it,
or the health URL answers 503:

docker compose restart nginx


### Unpaid bookings are not expiring

Exactly one `beat` container must run. It logs `Sending due task expire-pending-bookings` every 30 seconds,
and the worker logs the task as succeeded.

docker compose ps beat worker
docker compose logs --tail 20 beat
docker compose logs --tail 20 worker


To expire overdue bookings once by hand:

docker compose exec -T web python manage.py shell

from apps.bookings import booking_services, holds
booking_services.expire_pending(holds.now())


### Bookings waiting for a refund

A booking in `refund_required` means money arrived but the booking cannot stand (paid after cancelling, or
paid after expiry when the seats were taken). List them, refund at the gateway, and record it outside the
system for now (there is no `refunded` state yet):

docker compose exec -T web python manage.py shell

from apps.bookings.models import Booking
for b in Booking.objects.filter(status="refund_required").order_by(“updated_at”):
... paid = list(b.payments.filter(status="succeeded").values_list(“provider_ref”, “amount_pkr”))
... print(b.id, b.run_id, b.total_pkr, paid)


Application logs carry `payments.refund_required`, `payments.duplicate_charge` and `payments.amount_mismatch`
lines (booking or payment ids only) for the same cases.

### Rotating the webhook secret

1. Generate 32 random bytes as hex (the Phase 7 bootstrap block shows one way) and put it in `.env` as `PAYMENT_WEBHOOK_SECRET`.
2. Set the same value at the gateway.
3. `docker compose up -d`, so `web`, `worker` and `beat` are recreated with it, then `docker compose restart nginx`.

Events signed with the old secret during the switch get 400 and the gateway retries them. With no secret set,
the webhook answers 503 and settles nothing.

## Load tests, overload and throttles (Phase 8)

### Running a load test

The harness is in `loadtest/`; tokens and results are written outside the repository, in `..\railrush-loadtest`.

    .\loadtest\up.ps1                                     load-test mode (nginx trusts X-Forwarded-For, 60 s payment window)
    .\loadtest\rushcheck.ps1 -Users 400 -Runs 2 -Label x  one run, one result row, data invariants checked
    .\loadtest\down.ps1                                   always finish with this: back to the production configuration

Never use load-test mode in production: it trusts a client-supplied address and shortens the payment window. Compare
only runs from the same session: `loadtest\bench_cpu.py` shows how fast the machine is at that moment.

### When the site is overloaded

Symptoms: p95 above a few seconds, nginx 503 or 504, a growing queue.

1. Check CPU per container with `docker stats --no-stream`. Web at its ceiling means too many admissions per second
   for this hardware (see docs/CAPACITY.md for the sizing formula).
2. Lower `WAITROOM_ADMIT_PER_SECOND` in `.env` (it is per run) and run `docker compose up -d web`. It applies to people
   who join afterwards; tickets already issued keep their admission time.
3. Scale web (more containers or more CPU) before PostgreSQL or Redis: web is about 70% of the CPU.
4. If throttling misbehaves, set `FAST_THROTTLES=false` in `.env` and recreate web to fall back to DRF's throttles.
