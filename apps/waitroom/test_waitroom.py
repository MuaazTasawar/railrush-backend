"""Waiting-room tests.

Tests that touch Redis need a reachable Redis. The Docker test service has one and sets
REQUIRE_REDIS_TESTS=1, so they can never be skipped there; on a PC without Redis they skip.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import pairwise

import pytest
from django.core.exceptions import ImproperlyConfigured
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.catalog.models import TrainRun

from . import passes, services
from .permissions import HasWaitingRoomPass

pytestmark = pytest.mark.django_db

STATUS_FIELDS = {
    "run_id",
    "state",
    "opens_at",
    "joins_open_at",
    "admit_per_second",
    "tickets_issued",
    "people_waiting",
    "estimated_wait_seconds",
    "server_time",
}


def join_url(run):
    return f"/api/waitroom/{run.id}/join/"


def me_url(run):
    return f"/api/waitroom/{run.id}/me/"


def status_url(run):
    return f"/api/waitroom/{run.id}/status/"


def error_code(response):
    return response.json()["error"]["code"]


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def user_key(settings, run, user):
    return f"{settings.WAITROOM_KEY_PREFIX}:{{{run.id}}}:u:{user.id}"


# --- Joining and admission -------------------------------------------------------------


def test_join_and_me_require_sign_in(api, make_run):
    run = make_run()
    assert api.post(join_url(run)).status_code == 401
    assert api.get(me_url(run)).status_code == 401


def test_cannot_join_before_the_prequeue_window(api, redis_client, make_run, make_user, auth):
    run = make_run(opens_in=7200)
    response = api.post(join_url(run), **auth(make_user(1)))
    assert response.status_code == 409
    assert "not open" in response.json()["error"]["message"]
    assert api.get(status_url(run)).json()["state"] == "opening_soon"


def test_prequeue_joiners_wait_for_opening_then_get_a_pass(
    api, redis_client, clock, make_run, make_user, auth, settings
):
    run = make_run(opens_in=300)
    user = make_user(1)
    first = api.post(join_url(run), **auth(user))
    assert first.status_code == 201
    body = first.json()
    assert body["state"] == "waiting"
    assert body["ticket"] == 1
    assert body["wait_seconds"] == pytest.approx(300, abs=1)
    assert "waitroom_pass" not in body
    assert "no-store" in first["Cache-Control"]

    clock.advance(299)
    assert api.get(me_url(run), **auth(user)).json()["state"] == "waiting"

    clock.advance(2)
    admitted = api.get(me_url(run), **auth(user)).json()
    assert admitted["state"] == "admitted"
    assert passes.verify_pass(admitted["waitroom_pass"], user.id, run.id, clock())
    window = parse(admitted["pass_expires_at"]) - parse(admitted["admit_at"])
    assert window == pytest.approx(settings.WAITROOM_PASS_TTL_SECONDS, abs=1)


def test_joining_twice_returns_the_same_ticket(api, redis_client, make_run, make_user, auth, settings):
    run = make_run()
    headers = auth(make_user(1))
    first = api.post(join_url(run), **headers)
    second = api.post(join_url(run), **headers)
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json()["ticket"] == second.json()["ticket"] == 1
    assert redis_client.get(f"{settings.WAITROOM_KEY_PREFIX}:{{{run.id}}}:issued") == "1"


def test_an_open_idle_room_admits_the_first_joiner_immediately(
    api, redis_client, make_run, make_user, auth
):
    run = make_run(opens_in=-60)
    response = api.post(join_url(run), **auth(make_user(1)))
    assert response.status_code == 201
    assert response.json()["state"] == "admitted"
    assert "waitroom_pass" in response.json()


def test_admissions_are_spaced_by_the_configured_rate(redis_client, clock, make_run, settings):
    settings.WAITROOM_ADMIT_PER_SECOND = 10.0
    info = services.get_run_info(make_run(opens_in=-60).id)
    slots = [services.join(info, f"user-{i}")[0].admit_at for i in range(5)]
    assert slots[0] == pytest.approx(clock.t, abs=1e-3)
    assert [b - a for a, b in pairwise(slots)] == pytest.approx([0.1] * 4, abs=1e-3)


def test_joiners_before_opening_queue_up_from_the_opening_time(redis_client, clock, make_run, settings):
    settings.WAITROOM_ADMIT_PER_SECOND = 10.0
    run = make_run(opens_in=300)
    info = services.get_run_info(run.id)
    opens_at = clock.t + 300
    slots = [services.join(info, f"user-{i}")[0].admit_at for i in range(3)]
    assert slots == pytest.approx([opens_at, opens_at + 0.1, opens_at + 0.2], abs=1e-3)


def test_a_burst_after_an_idle_period_is_still_paced(redis_client, clock, make_run, settings):
    settings.WAITROOM_ADMIT_PER_SECOND = 10.0
    info = services.get_run_info(make_run(opens_in=-60).id)
    services.join(info, "early")
    clock.advance(1000)
    slots = [services.join(info, f"late-{i}")[0].admit_at for i in range(3)]
    assert slots == pytest.approx([clock.t, clock.t + 0.1, clock.t + 0.2], abs=1e-3)


def test_a_pass_window_ends_and_the_record_expires_in_redis(
    api, redis_client, clock, make_run, make_user, auth, settings
):
    run = make_run(opens_in=-60)
    user = make_user(1)
    admitted = api.post(join_url(run), **auth(user)).json()
    assert admitted["state"] == "admitted"
    clock.advance(settings.WAITROOM_PASS_TTL_SECONDS + 1)
    expired = api.get(me_url(run), **auth(user)).json()
    assert expired["state"] == "expired"
    assert "waitroom_pass" not in expired
    assert not passes.verify_pass(admitted["waitroom_pass"], user.id, run.id, clock())
    ttl = redis_client.ttl(user_key(settings, run, user))
    assert 0 < ttl <= settings.WAITROOM_PASS_TTL_SECONDS + services.RECORD_GRACE_SECONDS + 1


def test_me_without_joining_is_a_404_and_tickets_are_private(
    api, redis_client, make_run, make_user, auth
):
    run = make_run()
    api.post(join_url(run), **auth(make_user(1)))
    response = api.get(me_url(run), **auth(make_user(2)))
    assert response.status_code == 404
    assert error_code(response) == "not_found"


def test_a_full_queue_refuses_new_joiners_but_not_existing_ones(
    api, redis_client, make_run, make_user, auth, settings
):
    settings.WAITROOM_MAX_QUEUE = 2
    run = make_run()
    first = make_user(1)
    assert api.post(join_url(run), **auth(first)).status_code == 201
    assert api.post(join_url(run), **auth(make_user(2))).status_code == 201
    refused = api.post(join_url(run), **auth(make_user(3)))
    assert refused.status_code == 503
    assert error_code(refused) == "unavailable"
    assert api.post(join_url(run), **auth(User.objects.get(pk=first.pk))).status_code == 200


def test_cancelled_and_departed_runs_close_the_room(api, redis_client, make_run, make_user, auth):
    cancelled = make_run(status=TrainRun.Status.CANCELLED)
    departed = make_run(opens_in=-7200, departs_in=-60)
    for run in (cancelled, departed):
        assert api.get(status_url(run)).json()["state"] == "closed"
        response = api.post(join_url(run), **auth(make_user(run.id)))
        assert response.status_code == 409
        assert "closed" in response.json()["error"]["message"]


def test_unknown_runs_are_404(api, redis_client, make_user, auth):
    user = make_user(1)
    assert api.get("/api/waitroom/999999/status/").status_code == 404
    assert api.post("/api/waitroom/999999/join/", **auth(user)).status_code == 404
    response = api.get("/api/waitroom/999999/me/", **auth(user))
    assert error_code(response) == "not_found"
    assert api.get(f"/api/waitroom/{'9' * 25}/status/").status_code == 404


def test_join_is_throttled_per_user(api, redis_client, make_run, make_user, auth, settings):
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    limit = int(rates["waitroom_join"].split("/")[0])
    run = make_run()
    headers = auth(make_user(1))
    for _ in range(limit):
        assert api.post(join_url(run), **headers).status_code in (200, 201)
    blocked = api.post(join_url(run), **headers)
    assert blocked.status_code == 429
    assert error_code(blocked) == "throttled"


# --- Public status ---------------------------------------------------------------------------


def test_public_status_is_anonymous_cacheable_and_counts_the_queue(
    api, redis_client, make_run, make_user, auth, settings
):
    run = make_run(opens_in=300)
    empty = api.get(status_url(run), HTTP_AUTHORIZATION="Bearer junk")
    assert empty.status_code == 200
    assert set(empty.json()) == STATUS_FIELDS
    assert empty.json()["tickets_issued"] == 0
    assert "public" in empty["Cache-Control"]
    assert "max-age=1" in empty["Cache-Control"]

    for number in (1, 2, 3):
        api.post(join_url(run), **auth(make_user(number)))
    body = api.get(status_url(run)).json()
    assert body["state"] == "open"
    assert body["tickets_issued"] == 3
    assert body["people_waiting"] == 3
    expected = 300 + 3 / settings.WAITROOM_ADMIT_PER_SECOND
    assert body["estimated_wait_seconds"] == pytest.approx(expected, abs=1)


def test_public_status_rejects_unknown_query_parameters(api, redis_client, make_run):
    run = make_run()
    response = api.get(status_url(run), {"cachebuster": "evil-value"})
    assert response.status_code == 400
    assert "evil-value" not in response.content.decode()


# --- Passes and the permission class ----------------------------------------------------------


def test_passes_are_bound_to_one_user_and_one_run(clock):
    token = passes.issue_pass("user-a", 7, clock.t + 600)
    assert passes.verify_pass(token, "user-a", 7, clock())
    assert not passes.verify_pass(token, "user-b", 7, clock())
    assert not passes.verify_pass(token, "user-a", 8, clock())
    assert not passes.verify_pass(token, "user-a", 7, clock.t + 601)
    flipped = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
    assert not passes.verify_pass(flipped, "user-a", 7, clock())
    assert not passes.verify_pass("not-a-pass", "user-a", 7, clock())
    assert not passes.verify_pass("", "user-a", 7, clock())
    assert not passes.verify_pass("x" * 5000, "user-a", 7, clock())


def test_a_login_token_is_never_accepted_as_a_pass(make_user, clock):
    user = make_user(1)
    access = str(RefreshToken.for_user(user).access_token)
    assert not passes.verify_pass(access, user.id, 7, clock())


class GuardedView(APIView):
    permission_classes = [IsAuthenticated, HasWaitingRoomPass]

    def get(self, request, run_id):
        return Response({"ok": True})


def call_guarded(user, run_id, token=None):
    extra = {"HTTP_X_WAITROOM_PASS": token} if token else {}
    request = APIRequestFactory().get("/guarded/", **extra)
    force_authenticate(request, user=user)
    return GuardedView.as_view()(request, run_id=str(run_id))


def test_permission_class_needs_a_valid_pass_for_this_user_and_run(make_user, clock):
    user, other = make_user(1), make_user(2)
    good = passes.issue_pass(user.id, 7, clock.t + 600)
    assert call_guarded(user, 7, good).status_code == 200
    assert call_guarded(user, 7).status_code == 403
    assert call_guarded(user, 8, good).status_code == 403
    assert call_guarded(other, 7, good).status_code == 403
    expired = passes.issue_pass(user.id, 7, clock.t - 1)
    assert call_guarded(user, 7, expired).status_code == 403


# --- Failure and concurrency --------------------------------------------------------------------


def test_a_redis_outage_is_a_clean_503(api, make_run, make_user, auth, monkeypatch):
    run = make_run()
    user = make_user(1)

    def down():
        raise RedisConnectionError("internal-host.example:6379 refused the connection")

    monkeypatch.setattr(services, "get_redis", down)
    responses = [
        api.post(join_url(run), **auth(user)),
        api.get(me_url(run), **auth(user)),
        api.get(status_url(run)),
    ]
    for response in responses:
        assert response.status_code == 503
        assert error_code(response) == "unavailable"
        assert b"internal-host" not in response.content


def test_concurrent_joins_get_unique_evenly_spaced_slots(redis_client, clock, make_run, settings):
    settings.WAITROOM_ADMIT_PER_SECOND = 10.0
    info = services.get_run_info(make_run(opens_in=-60).id)
    with ThreadPoolExecutor(max_workers=16) as pool:
        tickets = list(pool.map(lambda i: services.join(info, f"user-{i}")[0], range(60)))
    ordered = sorted(tickets, key=lambda ticket: ticket.number)
    assert [ticket.number for ticket in ordered] == list(range(1, 61))
    slots = [ticket.admit_at for ticket in ordered]
    assert [b - a for a, b in pairwise(slots)] == pytest.approx([0.1] * 59, abs=1e-3)


def test_concurrent_joins_by_one_user_create_one_ticket(redis_client, clock, make_run, settings):
    run = make_run(opens_in=-60)
    info = services.get_run_info(run.id)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: services.join(info, "same-user"), range(30)))
    assert {ticket.number for ticket, _ in results} == {1}
    assert sum(1 for _, created in results if created) == 1
    assert redis_client.get(f"{settings.WAITROOM_KEY_PREFIX}:{{{run.id}}}:issued") == "1"


def test_a_zero_admission_rate_is_refused(settings):
    settings.WAITROOM_ADMIT_PER_SECOND = 0
    with pytest.raises(ImproperlyConfigured):
        services.admit_interval()


def test_a_stored_slot_rounded_up_by_microseconds_still_counts_as_admitted(
    api, redis_client, clock, make_run, make_user, auth
):
    # 1_900_000_000.0000007 is stored as .000001 (rounded up) by the Lua script's 6-decimal format.
    clock.t = 1_900_000_000.0000007
    run = make_run(opens_in=-60)
    body = api.post(join_url(run), **auth(make_user(1))).json()
    assert body["state"] == "admitted"
    assert "waitroom_pass" in body
