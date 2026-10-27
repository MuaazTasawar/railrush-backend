import json
import re
import uuid
from datetime import datetime, timezone

from django.test import Client
from prometheus_client import REGISTRY

from . import gateway
from .models import Booking, Payment


def sample(body, name, labels=""):
    match = re.search(r"^" + re.escape(name + labels) + r" ([0-9.e+-]+)$", body, re.M)
    return float(match.group(1)) if match else None


def test_state_gauges_count_bookings_and_overdue_pending(db, clock, make_user, make_run):
    run = make_run()
    user = make_user(1)

    def add(status, expires_in, key):
        return Booking.objects.create(
            user=user,
            run=run,
            status=status,
            total_pkr=2500,
            idempotency_key=key,
            expires_at=datetime.fromtimestamp(clock.t + expires_in, tz=timezone.utc),
        )

    add(Booking.Status.PENDING_PAYMENT, -600, "metrics-key-0001")  # overdue
    add(Booking.Status.PENDING_PAYMENT, 300, "metrics-key-0002")  # still open
    add(Booking.Status.REFUND_REQUIRED, -600, "metrics-key-0003")
    body = Client().get("/internal/metrics").content.decode()
    assert sample(body, "railrush_bookings", '{status="pending_payment"}') == 2.0
    assert sample(body, "railrush_bookings", '{status="refund_required"}') == 1.0
    assert sample(body, "railrush_bookings", '{status="confirmed"}') == 0.0
    assert sample(body, "railrush_pending_bookings_overdue") == 1.0


def test_webhook_results_are_counted(db, settings, clock, make_user, make_run):
    secret = "metrics-test-secret"  # pragma: allowlist secret
    settings.PAYMENT_WEBHOOK_SECRET = secret
    booking = Booking.objects.create(
        user=make_user(1),
        run=make_run(),
        total_pkr=2500,
        idempotency_key="metrics-key-0004",
        expires_at=datetime.fromtimestamp(clock.t + 600, tz=timezone.utc),
    )
    payment = Payment.objects.create(
        booking=booking, provider="mock", provider_ref=f"mock_{uuid.uuid4().hex}", amount_pkr=2500
    )
    labels = {"result": "confirmed"}
    before = REGISTRY.get_sample_value("railrush_payment_events_total", labels) or 0.0
    raw = json.dumps(
        {"provider_ref": payment.provider_ref, "status": "succeeded", "amount_pkr": 2500}
    ).encode()
    response = Client().generic(
        "POST",
        "/api/payments/webhook/",
        raw,
        content_type="application/json",
        HTTP_X_GATEWAY_SIGNATURE=gateway.sign(secret, raw),
    )
    assert response.status_code == 200
    assert response.json()["result"] == "confirmed"
    assert REGISTRY.get_sample_value("railrush_payment_events_total", labels) == before + 1
