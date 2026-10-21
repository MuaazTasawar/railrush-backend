

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
