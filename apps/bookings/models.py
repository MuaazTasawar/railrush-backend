"""Bookings, the seats they own, and payments.

Prices are whole Pakistani rupees, matching catalog.Fare. The partial unique constraint on
BookingSeat is the hard guard against selling a seat twice: Redis holds are the fast first gate,
but this constraint still holds if Redis state is lost.
"""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.catalog.models import SeatClass, TrainRun


class Booking(models.Model):
    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        REFUND_REQUIRED = "refund_required", "Refund required"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings"
    )
    run = models.ForeignKey(TrainRun, on_delete=models.PROTECT, related_name="bookings")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT
    )
    total_pkr = models.PositiveIntegerField()
    idempotency_key = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"], name="booking_user_recent_idx"),
            models.Index(fields=["status", "expires_at"], name="booking_expiry_idx"),
            models.Index(fields=["run", "status"], name="booking_run_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "idempotency_key"], name="uniq_booking_idempotency_per_user"
            ),
            models.CheckConstraint(condition=Q(total_pkr__gt=0), name="booking_total_positive"),
        ]

    def __str__(self):
        return f"Booking {self.id} ({self.status})"


class BookingSeat(models.Model):
    """One seat on one run. While active, no other booking can hold the same run and seat."""

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="seats")
    run = models.ForeignKey(TrainRun, on_delete=models.PROTECT, related_name="booked_seats")
    seat_label = models.CharField(max_length=8)
    seat_class = models.CharField(max_length=20, choices=SeatClass.choices)
    price_pkr = models.PositiveIntegerField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["run", "active"], name="bookingseat_run_active_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "seat_label"],
                condition=Q(active=True),
                name="uniq_active_seat_per_run",
            ),
            models.CheckConstraint(condition=Q(price_pkr__gt=0), name="bookingseat_price_positive"),
        ]

    def __str__(self):
        return f"{self.seat_label} on run {self.run_id}"


class Payment(models.Model):
    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(Booking, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=20)
    provider_ref = models.CharField(max_length=64, unique=True)
    amount_pkr = models.PositiveIntegerField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INITIATED)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["booking", "status"], name="payment_booking_status_idx")]
        constraints = [
            models.CheckConstraint(condition=Q(amount_pkr__gt=0), name="payment_amount_positive")
        ]

    def __str__(self):
        return f"Payment {self.provider_ref} ({self.status})"
