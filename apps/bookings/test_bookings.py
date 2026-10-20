import itertools
import json
import threading
import uuid
from datetime import UTC, datetime

import pytest
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction

from apps.catalog.models import Fare, SeatClass, TrainRun

from . import booking_services as services
from . import booking_tasks as tasks
from . import gateway
from .models import Booking, BookingSeat, Payment

SECRET = "test-webhook-secret"  # pragma: allowlist secret
LIVE = (Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED)


@pytest.fixture(autouse=True)
def _webhook_secret(settings):
    settings.PAYMENT_WEBHOOK_SECRET = SECRET


@pytest.fixture
def run(make_run):
    run = make_run()
    Fare.objects.create(train=run.train, seat_class=SeatClass.ECONOMY, amount_pkr=2500)
    Fare.objects.create(train=run.train, seat_class=SeatClass.AC_BUSINESS, amount_pkr=8500)
    return run


@pytest.fixture
def hold(api, headers):
    def call(user, run, seats):
        response = api.post(
            f"/api/holds/{run.id}/", {"seats": seats}, format="json", **headers(user, run)
        )
        assert response.status_code in (200, 201), response.content
        return response

    return call


@pytest.fixture
def book(api, auth, django_capture_on_commit_callbacks):
    def call(user, run, key="booking-key-0001"):
        with django_capture_on_commit_callbacks(execute=True):
            return api.post(
                "/api/bookings/",
                {"run_id": run.id},
                format="json",
                HTTP_IDEMPOTENCY_KEY=key,
                **auth(user),
            )

    return call


@pytest.fixture
def seed(clock):
    """Create a booking directly (no Redis), priced as economy seats at 2500 each."""
    counter = itertools.count(1)

    def factory(user, run, labels=("1-1A",), status=Booking.Status.PENDING_PAYMENT, expires_in=600):
        booking = Booking.objects.create(
            user=user,
            run=run,
            status=status,
            total_pkr=2500 * len(labels),
            idempotency_key=f"seed-key-{next(counter):04d}",
            expires_at=datetime.fromtimestamp(clock.t + expires_in, tz=UTC),
        )
        BookingSeat.objects.bulk_create(
            [
                BookingSeat(
                    booking=booking,
                    run=run,
                    seat_label=label,
                    seat_class=SeatClass.ECONOMY,
                    price_pkr=2500,
                    active=status in LIVE,
                )
                for label in labels
            ]
        )
        return booking

    return factory


def make_payment(booking, amount=None):
    return Payment.objects.create(
        booking=booking,
        provider="mock",
        provider_ref=f"mock_{uuid.uuid4().hex}",
        amount_pkr=amount or booking.total_pkr,
    )


def event(payment, status="succeeded", amount=None):
    body = {
        "provider_ref": payment.provider_ref,
        "status": status,
        "amount_pkr": payment.amount_pkr if amount is None else amount,
    }
    return json.dumps(body).encode()


def post_webhook(api, raw, signature=None):
    if signature is None:
        signature = gateway.sign(SECRET, raw)
    return api.generic(
        "POST",
        "/api/payments/webhook/",
        raw,
        content_type="application/json",
        HTTP_X_GATEWAY_SIGNATURE=signature,
    )


def active_labels(booking):
    return sorted(booking.seats.filter(active=True).values_list("seat_label", flat=True))


# ---------------------------------------------------------------- creating bookings


def test_booking_is_built_from_my_holds(api, auth, redis_client, run, make_user, hold, book):
    user = make_user(1)
    hold(user, run, ["1-1A", "1-1B", "2-1A"])
    response = book(user, run)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending_payment"
    assert body["total_pkr"] == 2500 * 2 + 8500
    assert sorted(seat["seat"] for seat in body["seats"]) == ["1-1A", "1-1B", "2-1A"]
    # The holds are released, and the seat map now shows the seats as taken.
    assert api.get(f"/api/holds/{run.id}/", **auth(user)).json()["seats"] == []
    coaches = api.get(f"/api/catalog/runs/{run.id}/seats/").json()["coaches"]
    unavailable = {coach["number"]: coach["unavailable"] for coach in coaches}
    assert unavailable[1] == ["1-1A", "1-1B"]
    assert unavailable[2] == ["2-1A"]


def test_booking_needs_sign_in(api, run):
    response = api.post(
        "/api/bookings/", {"run_id": run.id}, format="json", HTTP_IDEMPOTENCY_KEY="anon-key-0001"
    )
    assert response.status_code == 401


def test_idempotency_key_is_required_and_checked(api, auth, make_user, run):
    user = make_user(1)
    for header in ({}, {"HTTP_IDEMPOTENCY_KEY": "short"}, {"HTTP_IDEMPOTENCY_KEY": "bad key with spaces!"}):
        response = api.post("/api/bookings/", {"run_id": run.id}, format="json", **auth(user), **header)
        assert response.status_code == 400


def test_booking_without_holds_is_a_conflict(redis_client, run, make_user, book):
    response = book(make_user(1), run)
    assert response.status_code == 409
    assert Booking.objects.count() == 0


def test_retrying_with_the_same_key_returns_the_same_booking(redis_client, run, make_user, hold, book):
    user = make_user(1)
    hold(user, run, ["1-1A"])
    first = book(user, run, "retry-key-0001")
    second = book(user, run, "retry-key-0001")
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["id"] == second.json()["id"]
    assert Booking.objects.count() == 1


def test_a_key_cannot_be_reused_for_another_run(redis_client, run, make_run, make_user, hold, book):
    user = make_user(1)
    other = make_run()
    hold(user, run, ["1-1A"])
    assert book(user, run, "shared-key-0001").status_code == 201
    hold(user, other, ["1-1A"])
    assert book(user, other, "shared-key-0001").status_code == 409


def test_client_cannot_choose_the_price(api, auth, redis_client, run, make_user, hold):
    user = make_user(1)
    hold(user, run, ["1-1A"])
    response = api.post(
        "/api/bookings/",
        {"run_id": run.id, "total_pkr": 1, "price_pkr": 1, "seats": ["2-1A"]},
        format="json",
        HTTP_IDEMPOTENCY_KEY="tamper-key-0001",
        **auth(user),
    )
    assert response.status_code == 201
    assert response.json()["total_pkr"] == 2500
    assert [seat["seat"] for seat in response.json()["seats"]] == ["1-1A"]


def test_closed_run_refuses_booking(redis_client, run, make_user, hold, book):
    user = make_user(1)
    hold(user, run, ["1-1A"])
    TrainRun.objects.filter(pk=run.pk).update(status=TrainRun.Status.CANCELLED)
    cache.clear()
    assert book(user, run).status_code == 409
    assert Booking.objects.count() == 0


def test_a_seat_without_a_fare_is_never_sold_for_free(redis_client, run, make_user, hold, book):
    user = make_user(1)
    hold(user, run, ["1-1A"])
    Fare.objects.filter(seat_class=SeatClass.ECONOMY).delete()
    assert book(user, run).status_code == 409
    assert Booking.objects.count() == 0


def test_a_booked_seat_cannot_be_held_by_someone_else(api, headers, redis_client, run, make_user, hold, book):
    first, second = make_user(1), make_user(2)
    hold(first, run, ["1-1A"])
    assert book(first, run).status_code == 201
    response = api.post(
        f"/api/holds/{run.id}/", {"seats": ["1-1A"]}, format="json", **headers(second, run)
    )
    assert response.status_code == 409


def test_booking_endpoints_are_throttled(api, auth, make_user, run):
    user = make_user(1)
    codes = [
        api.post("/api/bookings/", {"run_id": run.id}, format="json", **auth(user)).status_code
        for _ in range(21)
    ]
    assert codes[:20] == [400] * 20
    assert codes[20] == 429


# ---------------------------------------------------------------- ownership (IDOR)


def test_other_people_cannot_see_pay_or_cancel_my_booking(api, auth, run, make_user, seed):
    owner, stranger = make_user(1), make_user(2)
    booking = seed(owner, run)
    base = f"/api/bookings/{booking.id}/"
    assert api.get(base, **auth(owner)).status_code == 200
    assert api.get(base, **auth(stranger)).status_code == 404
    assert api.post(base + "pay/", **auth(stranger)).status_code == 404
    assert api.post(base + "cancel/", **auth(stranger)).status_code == 404
    booking.refresh_from_db()
    assert booking.status == Booking.Status.PENDING_PAYMENT
    assert Payment.objects.count() == 0


def test_my_booking_list_only_contains_my_bookings(api, auth, run, make_user, seed):
    mine, theirs = make_user(1), make_user(2)
    own = seed(mine, run, ("1-1A",))
    seed(theirs, run, ("1-1B",))
    data = api.get("/api/bookings/", **auth(mine)).json()
    items = data["results"] if isinstance(data, dict) else data
    assert [item["id"] for item in items] == [str(own.id)]


# ---------------------------------------------------------------- paying and cancelling


def test_pay_opens_one_payment_and_reuses_it(api, auth, run, make_user, seed):
    user = make_user(1)
    booking = seed(user, run, ("1-1A", "1-1B"))
    first = api.post(f"/api/bookings/{booking.id}/pay/", **auth(user))
    second = api.post(f"/api/bookings/{booking.id}/pay/", **auth(user))
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["payment"]["id"] == second.json()["payment"]["id"]
    assert first.json()["payment"]["amount_pkr"] == 5000
    assert first.json()["checkout"]["reference"] == first.json()["payment"]["provider_ref"]
    assert Payment.objects.count() == 1


def test_pay_refuses_expired_or_cancelled_bookings(api, auth, run, make_user, seed):
    user = make_user(1)
    late = seed(user, run, ("1-1A",), expires_in=-1)
    cancelled = seed(user, run, ("1-1B",), status=Booking.Status.CANCELLED)
    for booking in (late, cancelled):
        assert api.post(f"/api/bookings/{booking.id}/pay/", **auth(user)).status_code == 409
    assert Payment.objects.count() == 0


def test_cancel_frees_the_seats_for_others(api, auth, headers, redis_client, run, make_user, hold, book):
    first, second = make_user(1), make_user(2)
    hold(first, run, ["1-1A"])
    booking_id = book(first, run).json()["id"]
    response = api.post(f"/api/bookings/{booking_id}/cancel/", **auth(first))
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert BookingSeat.objects.filter(active=True).count() == 0
    hold(second, run, ["1-1A"])  # sellable again
    # Cancelling twice is harmless.
    assert api.post(f"/api/bookings/{booking_id}/cancel/", **auth(first)).status_code == 200


def test_a_confirmed_booking_cannot_be_cancelled_here(api, auth, run, make_user, seed):
    user = make_user(1)
    booking = seed(user, run, status=Booking.Status.CONFIRMED)
    assert api.post(f"/api/bookings/{booking.id}/cancel/", **auth(user)).status_code == 409
    booking.refresh_from_db()
    assert booking.status == Booking.Status.CONFIRMED


# ---------------------------------------------------------------- webhook security


def test_forged_webhooks_are_rejected(api, run, make_user, seed):
    booking = seed(make_user(1), run)
    payment = make_payment(booking)
    raw = event(payment)
    tampered = event(payment, amount=1)
    attempts = [
        post_webhook(api, raw, signature="0" * 64),
        post_webhook(api, raw, signature=""),
        post_webhook(api, raw, signature=gateway.sign("some-other-secret", raw)),
        post_webhook(api, tampered, signature=gateway.sign(SECRET, raw)),
        post_webhook(api, raw, signature="\u00e9" * 64),
    ]
    assert [attempt.status_code for attempt in attempts] == [400] * 5
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == Booking.Status.PENDING_PAYMENT
    assert payment.status == Payment.Status.INITIATED


def test_webhooks_are_refused_when_no_secret_is_configured(api, settings, run, make_user, seed):
    payment = make_payment(seed(make_user(1), run))
    settings.PAYMENT_WEBHOOK_SECRET = ""
    raw = event(payment)
    assert post_webhook(api, raw, signature=gateway.sign("", raw)).status_code == 503
    payment.refresh_from_db()
    assert payment.status == Payment.Status.INITIATED


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        json.dumps({"provider_ref": "x"}).encode(),
        json.dumps({"provider_ref": "x", "status": "refunded", "amount_pkr": 1}).encode(),
        json.dumps({"provider_ref": "x", "status": "succeeded", "amount_pkr": "2500"}).encode(),
        json.dumps({"provider_ref": "x", "status": "succeeded", "amount_pkr": True}).encode(),
        json.dumps({"provider_ref": 7, "status": "succeeded", "amount_pkr": 1}).encode(),
    ],
)
def test_malformed_events_are_rejected(api, raw):
    assert post_webhook(api, raw).status_code == 400


def test_an_unknown_payment_is_a_404(api, db):
    raw = json.dumps({"provider_ref": "mock_nope", "status": "succeeded", "amount_pkr": 1}).encode()
    assert post_webhook(api, raw).status_code == 404


# ---------------------------------------------------------------- webhook behaviour


def test_a_signed_success_confirms_the_booking(api, run, make_user, seed):
    booking = seed(make_user(1), run, ("1-1A", "1-1B"))
    payment = make_payment(booking)
    response = post_webhook(api, event(payment))
    assert (response.status_code, response.json()["result"]) == (200, "confirmed")
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == Booking.Status.CONFIRMED and booking.confirmed_at is not None
    assert payment.status == Payment.Status.SUCCEEDED
    assert active_labels(booking) == ["1-1A", "1-1B"]


def test_replaying_a_webhook_changes_nothing(api, run, make_user, seed):
    booking = seed(make_user(1), run)
    payment = make_payment(booking)
    raw = event(payment)
    post_webhook(api, raw)
    booking.refresh_from_db()
    confirmed_at = booking.confirmed_at
    replay = post_webhook(api, raw)
    assert (replay.status_code, replay.json()["result"]) == (200, "duplicate")
    booking.refresh_from_db()
    assert booking.confirmed_at == confirmed_at
    assert Payment.objects.count() == 1


def test_a_wrong_amount_never_confirms(api, run, make_user, seed):
    booking = seed(make_user(1), run)
    payment = make_payment(booking)
    response = post_webhook(api, event(payment, amount=1))
    assert response.status_code == 400
    booking.refresh_from_db()
    payment.refresh_from_db()
    assert booking.status == Booking.Status.PENDING_PAYMENT
    assert payment.status == Payment.Status.FAILED


def test_a_failed_payment_leaves_the_booking_payable_again(api, auth, run, make_user, seed):
    user = make_user(1)
    booking = seed(user, run)
    first = api.post(f"/api/bookings/{booking.id}/pay/", **auth(user)).json()["payment"]
    payment = Payment.objects.get(pk=first["id"])
    assert post_webhook(api, event(payment, status="failed")).json()["result"] == "failed"
    booking.refresh_from_db()
    assert booking.status == Booking.Status.PENDING_PAYMENT
    retry = api.post(f"/api/bookings/{booking.id}/pay/", **auth(user))
    assert retry.status_code == 201
    assert retry.json()["payment"]["provider_ref"] != first["provider_ref"]


def test_payment_after_cancellation_is_flagged_for_refund(api, run, make_user, seed):
    booking = seed(make_user(1), run, status=Booking.Status.CANCELLED)
    payment = make_payment(booking)
    assert post_webhook(api, event(payment)).json()["result"] == "refund_required"
    booking.refresh_from_db()
    assert booking.status == Booking.Status.REFUND_REQUIRED
    assert active_labels(booking) == []


def test_a_late_payment_wins_its_seats_back_when_they_are_still_free(api, clock, run, make_user, seed):
    booking = seed(make_user(1), run, ("1-1A",), expires_in=60)
    payment = make_payment(booking)
    clock.advance(120)
    assert services.expire_pending(clock.t) == 1
    assert active_labels(booking) == []
    assert post_webhook(api, event(payment)).json()["result"] == "late_confirmed"
    booking.refresh_from_db()
    assert booking.status == Booking.Status.CONFIRMED
    assert active_labels(booking) == ["1-1A"]


def test_a_late_payment_is_refunded_when_someone_else_took_the_seats(api, clock, run, make_user, seed):
    late = seed(make_user(1), run, ("1-1A",), expires_in=60)
    payment = make_payment(late)
    clock.advance(120)
    services.expire_pending(clock.t)
    winner = seed(make_user(2), run, ("1-1A",))
    assert post_webhook(api, event(payment)).json()["result"] == "refund_required"
    late.refresh_from_db()
    winner.refresh_from_db()
    assert late.status == Booking.Status.REFUND_REQUIRED
    assert active_labels(late) == []
    assert winner.status == Booking.Status.PENDING_PAYMENT and active_labels(winner) == ["1-1A"]


# ---------------------------------------------------------------- expiry


def test_expiry_frees_unpaid_bookings_and_nothing_else(run, make_user, seed, clock):
    stale = seed(make_user(1), run, ("1-1A",), expires_in=60)
    fresh = seed(make_user(2), run, ("1-1B",), expires_in=900)
    paid = seed(make_user(3), run, ("1-1C",), status=Booking.Status.CONFIRMED, expires_in=-5000)
    clock.advance(120)
    assert tasks.expire_pending_bookings() == 1
    for booking in (stale, fresh, paid):
        booking.refresh_from_db()
    assert stale.status == Booking.Status.EXPIRED and active_labels(stale) == []
    assert fresh.status == Booking.Status.PENDING_PAYMENT and active_labels(fresh) == ["1-1B"]
    assert paid.status == Booking.Status.CONFIRMED and active_labels(paid) == ["1-1C"]
    assert tasks.expire_pending_bookings() == 0


def test_expiry_is_scheduled(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["expire-pending-bookings"]
    assert entry["task"] == "apps.bookings.tasks.expire_pending_bookings"
    assert entry["schedule"] <= 60


# ---------------------------------------------------------------- the database guard


def test_the_database_refuses_to_sell_a_seat_twice(run, make_user, seed):
    seed(make_user(1), run, ("1-1A",))
    with pytest.raises(IntegrityError), transaction.atomic():
        seed(make_user(2), run, ("1-1A",))
    # Nothing leaked from the failed attempt, and a cancelled booking's seat can be sold again.
    assert Booking.objects.count() == 1
    seed(make_user(3), run, ("1-2A",), status=Booking.Status.CANCELLED)
    seed(make_user(4), run, ("1-2A",))
    assert BookingSeat.objects.filter(seat_label="1-2A", active=True).count() == 1


@pytest.mark.django_db(transaction=True)
def test_twenty_buyers_race_for_one_seat_and_exactly_one_wins(run, make_user, clock):
    if connection.vendor != "postgresql":
        pytest.skip("The race needs real row-level concurrency (PostgreSQL).")
    users = [make_user(number) for number in range(20)]
    expires_at = datetime.fromtimestamp(clock.t + 600, tz=UTC)
    rows = [("1-1A", SeatClass.ECONOMY, 2500)]
    barrier = threading.Barrier(len(users))
    outcomes = []

    def attempt(user):
        try:
            barrier.wait(timeout=15)
            services._insert_booking(user.id, run.id, uuid.uuid4().hex, rows, 2500, expires_at)
            outcomes.append("won")
        except IntegrityError:
            outcomes.append("lost")
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(user,)) for user in users]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["lost"] * 19 + ["won"]
    assert Booking.objects.count() == 1
    assert BookingSeat.objects.filter(active=True).count() == 1
