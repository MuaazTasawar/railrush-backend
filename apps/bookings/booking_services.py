"""Booking and payment rules. Views stay thin; every state change happens here, under row locks.

Callers pass request.user, which with stateless JWT is a TokenUser and not a User row, so
everything here uses user.id and filters on user_id. Lock order is always booking first, then
payment, so two requests can never deadlock on them. All times come from holds.now(), the app's
single clock, so tests can move it.
"""

import logging
from datetime import UTC, datetime

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework.exceptions import NotFound

from apps.catalog.models import Fare
from apps.common.exceptions import Conflict

from . import holds, seatmap
from .gateway import get_gateway
from .models import Booking, BookingSeat, Payment

logger = logging.getLogger(__name__)


def _dt(timestamp):
    return datetime.fromtimestamp(timestamp, tz=UTC)


def _label_key(label):
    match = seatmap.LABEL_RE.match(label)
    return (int(match.group(1)), int(match.group(2)), match.group(3)) if match else (0, 0, label)


def _price_rows(layout, run_id, labels):
    """(label, seat class, price) per seat, priced here from the catalog and never from the client."""
    classes = {coach.number: coach.seat_class for coach in layout.coaches}
    fares = dict(
        Fare.objects.filter(train__runs__id=run_id).values_list("seat_class", "amount_pkr")
    )
    rows = []
    for label in labels:
        match = seatmap.LABEL_RE.match(label)
        seat_class = classes.get(int(match.group(1))) if match else None
        price = fares.get(seat_class)
        if not price:
            raise Conflict("A fare is not available for one of these seats.")
        rows.append((label, seat_class, price))
    return rows


def _insert_booking(user_id, run_id, key, rows, total, expires_at):
    """One transaction: the booking and all its seats, or nothing. The partial unique index on
    (run, seat) is the hard guard, so a seat that is already sold raises IntegrityError."""
    with transaction.atomic():
        booking = Booking.objects.create(
            user_id=user_id,
            run_id=run_id,
            total_pkr=total,
            idempotency_key=key,
            expires_at=expires_at,
        )
        BookingSeat.objects.bulk_create(
            [
                BookingSeat(
                    booking=booking,
                    run_id=run_id,
                    seat_label=label,
                    seat_class=seat_class,
                    price_pkr=price,
                )
                for label, seat_class, price in rows
            ]
        )
    return booking


def _replay(existing, run_id):
    if existing.run_id != run_id:
        raise Conflict("This Idempotency-Key was already used for a different run.")
    return existing


def create_booking(user, run_id, key):
    """Turn the user's live seat holds into a pending booking. Returns (booking, created)."""
    user_id = user.id
    existing = Booking.objects.filter(user_id=user_id, idempotency_key=key).first()
    if existing is not None:
        return _replay(existing, run_id), False

    at = holds.now()
    layout = seatmap.get_layout(run_id)
    if layout.cancelled or at >= layout.departs_at:
        raise Conflict("Booking is closed for this run.")
    if at < layout.opens_at:
        raise Conflict("Booking has not opened for this run yet.")

    held = holds.holds_for_user(run_id, user_id, at)
    if not held:
        raise Conflict("You have no active seat holds for this run. Hold seats first.")
    labels = sorted(held, key=_label_key)
    rows = _price_rows(layout, run_id, labels)
    total = sum(price for _, _, price in rows)
    expires_at = _dt(at + settings.BOOKING_PAYMENT_SECONDS)

    try:
        booking = _insert_booking(user_id, run_id, key, rows, total, expires_at)
    except IntegrityError as exc:
        # Either the same request raced itself, or a seat was sold in the meantime.
        existing = Booking.objects.filter(user_id=user_id, idempotency_key=key).first()
        if existing is not None:
            return _replay(existing, run_id), False
        raise Conflict("Some of those seats are no longer available.") from exc

    # The booking rows now reserve the seats, so the Redis holds are done. Release them only after
    # the commit: until then the holds are what keeps other buyers out.
    transaction.on_commit(
        lambda: holds.release(run_id, user_id, labels, holds.now()), robust=True
    )
    return booking, True


def _owned_locked(user, booking_id):
    try:
        return Booking.objects.select_for_update().get(pk=booking_id, user_id=user.id)
    except Booking.DoesNotExist as exc:
        raise NotFound("Booking not found.") from exc


def start_payment(user, booking_id):
    """Open (or reuse) a payment for an unpaid booking. Returns (payment, created)."""
    at_dt = _dt(holds.now())
    with transaction.atomic():
        booking = _owned_locked(user, booking_id)
        if booking.status != Booking.Status.PENDING_PAYMENT or booking.expires_at <= at_dt:
            raise Conflict("This booking is no longer open for payment.")
        payment = booking.payments.filter(status=Payment.Status.INITIATED).first()
        if payment is not None:
            return payment, False
        gateway = get_gateway()
        payment = Payment.objects.create(
            booking=booking,
            provider=gateway.name,
            provider_ref=gateway.new_reference(),
            amount_pkr=booking.total_pkr,
        )
    return payment, True


def cancel_booking(user, booking_id):
    with transaction.atomic():
        booking = _owned_locked(user, booking_id)
        if booking.status == Booking.Status.CANCELLED:
            return booking
        if booking.status != Booking.Status.PENDING_PAYMENT:
            raise Conflict("Only bookings that are still unpaid can be cancelled here.")
        booking.status = Booking.Status.CANCELLED
        booking.cancelled_at = _dt(holds.now())
        booking.save(update_fields=["status", "cancelled_at", "updated_at"])
        BookingSeat.objects.filter(booking=booking, active=True).update(active=False)
    return booking


def _confirm(booking, at_dt):
    booking.status = Booking.Status.CONFIRMED
    booking.confirmed_at = at_dt
    booking.save(update_fields=["status", "confirmed_at", "updated_at"])


def _settle(booking, at_dt):
    """A payment succeeded. Decide what that means for the booking, whatever state it is in."""
    if booking.status == Booking.Status.CONFIRMED:
        logger.error("payments.duplicate_charge booking=%s", booking.id)
        return "duplicate_charge"
    if booking.status == Booking.Status.PENDING_PAYMENT:
        _confirm(booking, at_dt)
        return "confirmed"
    if booking.status == Booking.Status.EXPIRED:
        try:
            with transaction.atomic():
                BookingSeat.objects.filter(booking=booking).update(active=True)
        except IntegrityError:
            booking.status = Booking.Status.REFUND_REQUIRED
            booking.save(update_fields=["status", "updated_at"])
            logger.error("payments.refund_required booking=%s reason=seats_taken", booking.id)
            return "refund_required"
        _confirm(booking, at_dt)
        return "late_confirmed"
    # Cancelled by the buyer (or already flagged): the money arrived but the booking will not be revived.
    if booking.status != Booking.Status.REFUND_REQUIRED:
        booking.status = Booking.Status.REFUND_REQUIRED
        booking.save(update_fields=["status", "updated_at"])
    logger.error("payments.refund_required booking=%s reason=not_open", booking.id)
    return "refund_required"


def apply_payment_event(provider_ref, succeeded, amount_pkr, payload):
    """Apply a verified gateway event. Safe to repeat: a payment is settled once, then ignored."""
    booking_id = (
        Payment.objects.filter(provider_ref=provider_ref).values_list("booking_id", flat=True).first()
    )
    if booking_id is None:
        raise NotFound("Unknown payment.")
    at_dt = _dt(holds.now())
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking_id)
        payment = Payment.objects.select_for_update().get(provider_ref=provider_ref)
        if payment.status != Payment.Status.INITIATED:
            return "duplicate"
        payment.payload = payload
        if not succeeded:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status", "payload", "updated_at"])
            return "failed"
        if amount_pkr != payment.amount_pkr:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status", "payload", "updated_at"])
            logger.error("payments.amount_mismatch payment=%s", payment.id)
            return "amount_mismatch"
        payment.status = Payment.Status.SUCCEEDED
        payment.save(update_fields=["status", "payload", "updated_at"])
        return _settle(booking, at_dt)


def expire_pending(at, batch=500):
    """Expire unpaid bookings past their deadline and free their seats. Returns how many."""
    at_dt = _dt(at)
    ids = list(
        Booking.objects.filter(status=Booking.Status.PENDING_PAYMENT, expires_at__lte=at_dt)
        .order_by("expires_at")
        .values_list("pk", flat=True)[:batch]
    )
    expired = 0
    for pk in ids:
        with transaction.atomic():
            booking = (
                Booking.objects.select_for_update(skip_locked=True)
                .filter(pk=pk, status=Booking.Status.PENDING_PAYMENT, expires_at__lte=at_dt)
                .first()
            )
            if booking is None:
                continue
            booking.status = Booking.Status.EXPIRED
            booking.save(update_fields=["status", "updated_at"])
            BookingSeat.objects.filter(booking=booking, active=True).update(active=False)
            expired += 1
    return expired
