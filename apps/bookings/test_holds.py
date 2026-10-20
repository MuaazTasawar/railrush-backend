"""Seat-hold tests. Tests that touch Redis need a reachable Redis; the Docker test service requires it."""

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework.exceptions import ValidationError

from apps.catalog.models import TrainRun
from apps.waitroom import passes

from . import holds, seatmap

pytestmark = pytest.mark.django_db

LAYOUT = seatmap.RunLayout(
    run_id=1,
    opens_at=0.0,
    departs_at=1e12,
    cancelled=False,
    coaches=(
        seatmap.CoachLayout(1, "economy", 3, "ABCD"),
        seatmap.CoachLayout(2, "ac_business", 2, "AB"),
    ),
)


def hold_url(run):
    return f"/api/holds/{run.id}/"


def release_url(run):
    return f"/api/holds/{run.id}/release/"


def map_url(run):
    return f"/api/catalog/runs/{run.id}/seats/"


def error(response):
    return response.json()["error"]


def parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def hold(api, headers, run, *seats):
    return api.post(hold_url(run), {"seats": list(seats)}, **headers)


def seat_names(response):
    return [item["seat"] for item in response.json()["seats"]]


# --- Seat names -----------------------------------------------------------------------------


def test_seat_names_are_validated_against_the_layout():
    assert seatmap.validate_labels(LAYOUT, ["1-1a", " 2-2B ", "1-03D"]) == ["1-1A", "2-2B", "1-3D"]
    for bad in ["9-1A", "1-4A", "1-0A", "1-1E", "2-1C", "abc", "", "1-1AA", "-1A", "1--1A"]:
        with pytest.raises(ValidationError):
            seatmap.validate_labels(LAYOUT, [bad])
    with pytest.raises(ValidationError):
        seatmap.validate_labels(LAYOUT, ["1-1A", "1-01A"])


# --- Holding --------------------------------------------------------------------------------


def test_holding_seats_returns_their_expiry(
    api, redis_client, clock, make_run, make_user, headers, settings
):
    run, user = make_run(), make_user(1)
    response = hold(api, headers(user, run), run, "1-1A", "1-1B")
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"run_id", "hold_seconds", "server_time", "seats"}
    assert seat_names(response) == ["1-1A", "1-1B"]
    for item in body["seats"]:
        left = parse(item["expires_at"]) - parse(body["server_time"])
        assert left == pytest.approx(settings.HOLD_SECONDS, abs=1)
    assert "no-store" in response["Cache-Control"]


def test_holding_needs_sign_in_and_a_pass_for_this_user_and_run(
    api, redis_client, clock, make_run, make_user, auth, headers
):
    run, other_run = make_run(), make_run()
    user, other = make_user(1), make_user(2)
    body = {"seats": ["1-1A"]}
    assert api.post(hold_url(run), body).status_code == 401
    assert api.post(hold_url(run), body, **auth(user)).status_code == 403
    wrong_run = {**auth(user), "HTTP_X_WAITROOM_PASS": passes.issue_pass(user.id, other_run.id, clock.t + 600)}
    wrong_user = {**auth(user), "HTTP_X_WAITROOM_PASS": passes.issue_pass(other.id, run.id, clock.t + 600)}
    expired = {**auth(user), "HTTP_X_WAITROOM_PASS": passes.issue_pass(user.id, run.id, clock.t - 1)}
    for bad in (wrong_run, wrong_user, expired):
        assert api.post(hold_url(run), body, **bad).status_code == 403
    assert api.post(hold_url(run), body, **headers(user, run)).status_code == 201


def test_a_request_containing_a_taken_seat_gets_nothing(api, redis_client, make_run, make_user, headers):
    run = make_run()
    first, second = make_user(1), make_user(2)
    assert hold(api, headers(first, run), run, "1-1A").status_code == 201
    response = hold(api, headers(second, run), run, "1-1B", "1-1A")
    assert response.status_code == 409
    assert error(response)["code"] == "seats_unavailable"
    assert error(response)["details"]["unavailable"] == ["1-1A"]
    assert api.get(hold_url(run), **headers(second, run)).json()["seats"] == []
    assert hold(api, headers(second, run), run, "1-1B").status_code == 201


def test_holding_your_own_seat_again_does_not_extend_it(
    api, redis_client, clock, make_run, make_user, headers
):
    run, user = make_run(), make_user(1)
    first = hold(api, headers(user, run), run, "1-1A")
    clock.advance(100)
    again = hold(api, headers(user, run), run, "1-1A")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["seats"] == first.json()["seats"]


def test_a_user_can_hold_at_most_the_configured_number_of_seats(
    api, redis_client, make_run, make_user, headers, settings
):
    run, user = make_run(), make_user(1)
    h = headers(user, run)
    assert hold(api, h, run, "1-1A", "1-1B").status_code == 201
    assert hold(api, h, run, "1-1C", "1-1D").status_code == 201
    blocked = hold(api, h, run, "1-2A")
    assert blocked.status_code == 409
    assert error(blocked)["code"] == "hold_limit_reached"
    assert error(blocked)["details"]["max_seats"] == settings.HOLD_MAX_SEATS_PER_USER
    assert hold(api, h, run, "1-2A", "1-2B", "1-2C", "1-2D", "1-3A").status_code == 400


def test_holds_expire_by_themselves(api, redis_client, clock, make_run, make_user, headers, settings):
    run = make_run()
    first, second = make_user(1), make_user(2)
    hold(api, headers(first, run), run, "1-1A")
    assert hold(api, headers(second, run), run, "1-1A").status_code == 409
    clock.advance(settings.HOLD_SECONDS + 1)
    assert api.get(hold_url(run), **headers(first, run)).json()["seats"] == []
    assert hold(api, headers(second, run), run, "1-1A").status_code == 201
    z_key, owner_key = holds._run_keys(run.id)
    assert redis_client.zcard(z_key) == 1
    assert redis_client.hlen(owner_key) == 1


PAYLOADS = [
    {},
    {"seats": []},
    {"seats": "1-1A"},
    {"seats": ["1-1A", "1-1A"]},
    {"seats": ["9-9Z"]},
    {"seats": ["x" * 50]},
    {"seats": [1, 2]},
    {"seats": ["EVIL-9Z"]},
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_bad_hold_requests_are_400_and_never_echoed(api, redis_client, make_run, make_user, headers, payload):
    run, user = make_run(), make_user(1)
    response = api.post(hold_url(run), payload, **headers(user, run))
    assert response.status_code == 400
    assert error(response)["code"] == "validation_error"
    assert "EVIL" not in response.content.decode()


def test_a_non_object_body_is_400(api, redis_client, make_run, make_user, headers):
    run, user = make_run(), make_user(1)
    response = api.post(hold_url(run), ["1-1A"], format="json", **headers(user, run))
    assert response.status_code == 400


def test_holds_are_refused_unless_booking_is_open(api, redis_client, make_run, make_user, headers):
    user = make_user(1)
    not_open = make_run(opens_in=600)
    cancelled = make_run(status=TrainRun.Status.CANCELLED)
    departed = make_run(opens_in=-7200, departs_in=-60)
    for run, word in ((not_open, "not opened"), (cancelled, "closed"), (departed, "closed")):
        response = hold(api, headers(user, run), run, "1-1A")
        assert response.status_code == 409
        assert word in error(response)["message"]


def test_holding_on_an_unknown_run_is_404(api, redis_client, clock, make_user, auth):
    user = make_user(1)
    pass_token = passes.issue_pass(user.id, 999999, clock.t + 600)
    response = api.post(
        "/api/holds/999999/", {"seats": ["1-1A"]}, **auth(user), HTTP_X_WAITROOM_PASS=pass_token
    )
    assert response.status_code == 404
    assert error(response)["code"] == "not_found"


# --- Releasing and listing ------------------------------------------------------------------


def test_releasing_frees_only_your_own_seats(api, redis_client, make_run, make_user, headers, auth):
    run = make_run()
    first, second = make_user(1), make_user(2)
    hold(api, headers(first, run), run, "1-1A", "1-1B")
    stranger = api.post(release_url(run), {"seats": ["1-1A"]}, **auth(second))
    assert stranger.status_code == 200
    assert stranger.json()["released"] == []
    assert len(api.get(hold_url(run), **auth(first)).json()["seats"]) == 2
    one = api.post(release_url(run), {"seats": ["1-1a"]}, **auth(first))
    assert one.json()["released"] == ["1-1A"]
    assert hold(api, headers(second, run), run, "1-1A").status_code == 201
    everything = api.post(release_url(run), {}, **auth(first))
    assert everything.json()["released"] == ["1-1B"]
    assert api.get(hold_url(run), **auth(first)).json()["seats"] == []


def test_listing_and_releasing_need_sign_in_but_not_a_pass(api, redis_client, make_run, make_user, auth):
    run, user = make_run(), make_user(1)
    assert api.get(hold_url(run)).status_code == 401
    assert api.post(release_url(run), {}).status_code == 401
    assert api.get(hold_url(run), **auth(user)).status_code == 200
    assert api.post(release_url(run), {}, **auth(user)).status_code == 200


def test_verify_holds_is_true_only_for_live_holds_by_that_user(
    redis_client, clock, make_run, make_user, settings
):
    run = make_run()
    first, second = make_user(1), make_user(2)
    holds.hold(run.id, first.id, ["1-1A", "1-1B"], clock())
    assert holds.verify_holds(run.id, first.id, ["1-1A", "1-1B"], clock())
    assert not holds.verify_holds(run.id, second.id, ["1-1A"], clock())
    assert not holds.verify_holds(run.id, first.id, ["1-1C"], clock())
    assert not holds.verify_holds(run.id, first.id, [], clock())
    clock.advance(settings.HOLD_SECONDS + 1)
    assert not holds.verify_holds(run.id, first.id, ["1-1A"], clock())


# --- The public seat map ----------------------------------------------------------------------


def test_seat_map_is_public_and_shows_taken_seats(api, redis_client, make_run, make_user, headers):
    run, user = make_run(), make_user(1)
    empty = api.get(map_url(run), HTTP_AUTHORIZATION="Bearer junk")
    assert empty.status_code == 200
    body = empty.json()
    assert set(body) == {"run_id", "hold_seconds", "server_time", "coaches", "availability"}
    assert body["coaches"] == [
        {"number": 1, "seat_class": "economy", "rows": 3, "columns": "ABCD", "unavailable": []},
        {"number": 2, "seat_class": "ac_business", "rows": 2, "columns": "AB", "unavailable": []},
    ]
    assert body["availability"] == [
        {"seat_class": "economy", "total": 12, "available": 12},
        {"seat_class": "ac_business", "total": 4, "available": 4},
    ]
    assert "public" in empty["Cache-Control"]
    assert "max-age=1" in empty["Cache-Control"]

    hold(api, headers(user, run), run, "1-1A", "1-3D", "2-2B")
    taken = api.get(map_url(run)).json()
    assert taken["coaches"][0]["unavailable"] == ["1-1A", "1-3D"]
    assert taken["coaches"][1]["unavailable"] == ["2-2B"]
    assert taken["availability"] == [
        {"seat_class": "economy", "total": 12, "available": 10},
        {"seat_class": "ac_business", "total": 4, "available": 3},
    ]


def test_seat_map_frees_seats_when_holds_expire(
    api, redis_client, clock, make_run, make_user, headers, settings
):
    run, user = make_run(), make_user(1)
    hold(api, headers(user, run), run, "1-1A")
    clock.advance(settings.HOLD_SECONDS + 1)
    assert api.get(map_url(run)).json()["coaches"][0]["unavailable"] == []


def test_seat_map_rejects_unknown_parameters_and_unknown_runs(api, redis_client, make_run):
    run = make_run()
    assert api.get(map_url(run), {"cachebuster": "1"}).status_code == 400
    missing = api.get("/api/catalog/runs/999999/seats/")
    assert missing.status_code == 404
    assert error(missing)["code"] == "not_found"


# --- Concurrency and failure ---------------------------------------------------------------------


def test_only_one_of_many_racing_users_gets_a_seat(redis_client, clock, make_run, make_user):
    run = make_run()
    users = [make_user(i) for i in range(40)]
    at = clock()
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda user: holds.hold(run.id, user.id, ["1-1A"], at), users))
    statuses = [result.status for result in results]
    assert statuses.count(holds.HELD) == 1
    assert statuses.count(holds.UNAVAILABLE) == 39


def test_multi_seat_requests_are_all_or_nothing_under_contention(redis_client, clock, make_run, make_user):
    run = make_run()
    seat_pool = ["1-1A", "1-1B", "1-1C", "1-1D", "1-2A", "1-2B"]
    rng = random.Random(7)
    wanted = [(make_user(i), rng.sample(seat_pool, 2)) for i in range(24)]
    at = clock()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda item: holds.hold(run.id, item[0].id, item[1], at), wanted))
    z_key, owner_key = holds._run_keys(run.id)
    owners = redis_client.hgetall(owner_key)
    assert redis_client.zcard(z_key) == len(owners)
    per_user = {}
    for seat, owner in owners.items():
        per_user.setdefault(owner, []).append(seat)
    assert all(len(seats) == 2 for seats in per_user.values())
    winners = {
        str(user.id)
        for (user, _), result in zip(wanted, results, strict=True)
        if result.status == holds.HELD
    }
    assert set(per_user) == winners


def test_the_per_user_limit_holds_under_concurrent_requests(redis_client, clock, make_run, make_user, settings):
    run, user = make_run(), make_user(1)
    seats = [f"1-{row}{column}" for row in (1, 2, 3) for column in "ABCD"]
    at = clock()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda seat: holds.hold(run.id, user.id, [seat], at), seats))
    assert [result.status for result in results].count(holds.HELD) == settings.HOLD_MAX_SEATS_PER_USER
    assert len(holds.holds_for_user(run.id, user.id, at)) == settings.HOLD_MAX_SEATS_PER_USER


def test_a_large_backlog_of_expired_holds_is_purged_safely(redis_client, clock, make_run, make_user):
    run, user = make_run(), make_user(1)
    z_key, owner_key = holds._run_keys(run.id)
    expired = {f"7-{n}A": clock.t - 10 for n in range(1200)}
    redis_client.zadd(z_key, expired)
    redis_client.hset(owner_key, mapping={label: "ghost" for label in expired})
    result = holds.hold(run.id, user.id, ["1-1A"], clock())
    assert result.status == holds.HELD
    assert redis_client.zcard(z_key) == 1
    assert redis_client.hlen(owner_key) == 1


def test_a_redis_outage_is_a_clean_503(api, make_run, make_user, headers, auth, monkeypatch):
    run, user = make_run(), make_user(1)

    def down():
        raise RedisConnectionError("internal-host.example:6379 refused the connection")

    monkeypatch.setattr(holds, "get_redis", down)
    responses = [
        hold(api, headers(user, run), run, "1-1A"),
        api.get(hold_url(run), **auth(user)),
        api.get(map_url(run)),
    ]
    for response in responses:
        assert response.status_code == 503
        assert error(response)["code"] == "unavailable"
        assert b"internal-host" not in response.content


def test_hold_requests_are_throttled_per_user(api, redis_client, make_run, make_user, headers, settings):
    limit = int(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["hold"].split("/")[0])
    run = make_run()
    h = headers(make_user(1), run)
    for _ in range(limit):
        assert hold(api, h, run, "1-1A").status_code in (200, 201)
    blocked = hold(api, h, run, "1-1A")
    assert blocked.status_code == 429
    assert error(blocked)["code"] == "throttled"
