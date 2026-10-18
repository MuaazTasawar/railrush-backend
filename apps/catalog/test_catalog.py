"""Catalog tests: constraints, public read endpoints, cache headers and the demo seed command."""

from datetime import datetime, time, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from .models import Coach, Fare, Seat, SeatClass, Station, Train, TrainRun
from .timeutils import local_today

pytestmark = pytest.mark.django_db

STATIONS_URL = "/api/catalog/stations/"
RUNS_URL = "/api/catalog/runs/"
KARACHI = ZoneInfo("Asia/Karachi")
RUN_FIELDS = {
    "id",
    "train_number",
    "train_name",
    "origin",
    "destination",
    "departure_at",
    "arrival_at",
    "booking_opens_at",
    "status",
    "classes",
}


def tomorrow():
    return local_today() + timedelta(days=1)


def make_run(train, day, hour=18, minute=30, **extra):
    departure = datetime.combine(day, time(hour, minute), tzinfo=KARACHI)
    fields = {
        "arrival_at": departure + timedelta(hours=18),
        "booking_opens_at": departure - timedelta(days=7),
    }
    fields.update(extra)
    return TrainRun.objects.create(train=train, departure_at=departure, **fields)


def search(api, origin="KHI", destination="LHE", day=None, **extra):
    day = day or tomorrow()
    params = {"origin": origin, "destination": destination, "date": day.isoformat(), **extra}
    return api.get(RUNS_URL, params)


def error_code(response):
    return response.json()["error"]["code"]


def seed(*args):
    out = StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


# --- Database rules -----------------------------------------------------------------


def test_coach_seat_count(network):
    coach = Coach.objects.get(train=network.train, number=3)
    assert coach.seat_count == 12


def test_train_cannot_start_and_end_at_the_same_station(network):
    with pytest.raises(IntegrityError), transaction.atomic():
        Train.objects.create(number="777", name="Loop", origin=network.khi, destination=network.khi)


def test_train_number_is_unique(network):
    with pytest.raises(IntegrityError), transaction.atomic():
        Train.objects.create(number="101", name="Dup", origin=network.khi, destination=network.pew)


def test_a_train_cannot_have_two_runs_at_the_same_departure(network):
    run = make_run(network.train, tomorrow())
    with pytest.raises(IntegrityError), transaction.atomic():
        TrainRun.objects.create(
            train=network.train,
            departure_at=run.departure_at,
            arrival_at=run.arrival_at,
            booking_opens_at=run.booking_opens_at,
        )


def test_run_must_arrive_after_it_departs(network):
    departure = datetime.combine(tomorrow(), time(9, 0), tzinfo=KARACHI)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrainRun.objects.create(
            train=network.train,
            departure_at=departure,
            arrival_at=departure - timedelta(hours=1),
            booking_opens_at=departure - timedelta(days=7),
        )


def test_booking_must_open_before_departure(network):
    departure = datetime.combine(tomorrow(), time(9, 0), tzinfo=KARACHI)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrainRun.objects.create(
            train=network.train,
            departure_at=departure,
            arrival_at=departure + timedelta(hours=5),
            booking_opens_at=departure + timedelta(hours=1),
        )


def test_fare_is_unique_per_class_and_positive(network):
    with pytest.raises(IntegrityError), transaction.atomic():
        Fare.objects.create(train=network.train, seat_class=SeatClass.ECONOMY, amount_pkr=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        Fare.objects.create(train=network.train, seat_class=SeatClass.AC_STANDARD, amount_pkr=0)


# --- Stations -------------------------------------------------------------------------


def test_stations_are_public_sorted_and_cacheable(api, network):
    response = api.get(STATIONS_URL, HTTP_AUTHORIZATION="Bearer not-a-real-token")
    assert response.status_code == 200
    body = response.json()
    assert [station["code"] for station in body] == ["KHI", "LHE", "PEW"]
    assert set(body[0]) == {"code", "name", "city"}
    assert "public" in response["Cache-Control"]
    assert "max-age=60" in response["Cache-Control"]


@pytest.mark.parametrize("url", [STATIONS_URL, RUNS_URL, f"{RUNS_URL}1/"])
def test_unsupported_query_parameters_are_rejected_without_echo(api, network, url):
    response = api.get(url, {"cachebuster": "evil-value"})
    assert response.status_code == 400
    assert error_code(response) == "validation_error"
    text = response.content.decode()
    assert "evil-value" not in text
    assert "cachebuster" not in text


# --- Search ---------------------------------------------------------------------------


def test_search_returns_runs_with_class_offers(api, network):
    make_run(network.train, tomorrow())
    response = search(api)
    assert response.status_code == 200
    assert "max-age=1" in response["Cache-Control"]
    body = response.json()
    assert body["count"] == 1
    run = body["results"][0]
    assert set(run) == RUN_FIELDS
    assert run["train_number"] == "101"
    assert run["origin"]["code"] == "KHI"
    assert run["destination"]["code"] == "LHE"
    assert run["status"] == "scheduled"
    assert run["classes"] == [
        {"seat_class": "economy", "fare_pkr": 2500, "seat_count": 60},
        {"seat_class": "ac_business", "fare_pkr": 8500, "seat_count": 12},
    ]


def test_search_codes_are_case_insensitive(api, network):
    make_run(network.train, tomorrow())
    assert search(api, origin="khi", destination=" lhe ").json()["count"] == 1


def test_search_uses_the_local_calendar_day(api, network):
    day = tomorrow()
    late = make_run(network.train, day, hour=23, minute=30)
    early_next_day = make_run(network.train, day + timedelta(days=1), hour=0, minute=30)
    first = search(api, day=day).json()
    second = search(api, day=day + timedelta(days=1)).json()
    assert [run["id"] for run in first["results"]] == [late.id]
    assert [run["id"] for run in second["results"]] == [early_next_day.id]


def test_search_hides_cancelled_runs_and_other_routes(api, network):
    make_run(network.train, tomorrow(), status=TrainRun.Status.CANCELLED)
    assert search(api).json()["count"] == 0
    make_run(network.train, tomorrow(), hour=9)
    assert search(api, origin="LHE", destination="KHI").json()["count"] == 0
    assert search(api, origin="ZZZ", destination="YYY").status_code == 200


def test_search_orders_by_departure_time(api, network):
    late = make_run(network.train, tomorrow(), hour=20)
    early = make_run(network.train, tomorrow(), hour=8)
    ids = [run["id"] for run in search(api).json()["results"]]
    assert ids == [early.id, late.id]


@pytest.mark.parametrize(
    "override",
    [
        {"origin": None},
        {"destination": None},
        {"date": None},
        {"date": "2026-13-40"},
        {"date": "tomorrow"},
        {"date": "PAST"},
        {"date": "FAR"},
        {"origin": "K"},
        {"origin": "KHIKHI"},
        {"origin": "K1"},
        {"destination": "L-E"},
    ],
)
def test_search_rejects_bad_parameters(api, network, override):
    params = {"origin": "KHI", "destination": "LHE", "date": tomorrow().isoformat()}
    for key, value in override.items():
        if value == "PAST":
            value = (local_today() - timedelta(days=1)).isoformat()
        elif value == "FAR":
            value = (local_today() + timedelta(days=settings.CATALOG_MAX_DAYS_AHEAD + 1)).isoformat()
        if value is None:
            params.pop(key)
        else:
            params[key] = value
    response = api.get(RUNS_URL, params)
    assert response.status_code == 400
    assert error_code(response) == "validation_error"


def count_queries(api, params):
    with CaptureQueriesContext(connection) as captured:
        response = api.get(RUNS_URL, params)
    assert response.status_code == 200
    return len(captured)


def test_search_query_count_does_not_grow_with_results(api, network):
    day = tomorrow()
    make_run(network.train, day)
    params = {"origin": "KHI", "destination": "LHE", "date": day.isoformat()}
    few = count_queries(api, params)
    for i in range(5):
        extra = Train.objects.create(
            number=f"9{i}", name=f"Extra {i}", origin=network.khi, destination=network.lhe
        )
        Coach.objects.create(train=extra, number=1, seat_class=SeatClass.ECONOMY, rows=2, columns="AB")
        Fare.objects.create(train=extra, seat_class=SeatClass.ECONOMY, amount_pkr=2000)
        make_run(extra, day, hour=8 + i)
    many = count_queries(api, params)
    assert few == many
    assert many <= 5


def test_search_is_paginated(api, network):
    day = tomorrow()
    for hour in range(21):
        make_run(network.train, day, hour=hour, minute=5)
    first = search(api).json()
    assert first["count"] == 21
    assert len(first["results"]) == 20
    assert first["next"] is not None
    assert len(search(api, page=2).json()["results"]) == 1


# --- Run detail -------------------------------------------------------------------------


def test_run_detail_includes_the_coach_layout(api, network):
    run = make_run(network.train, tomorrow())
    response = api.get(f"{RUNS_URL}{run.id}/")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == RUN_FIELDS | {"coaches"}
    assert body["coaches"] == [
        {"number": 1, "seat_class": "economy", "rows": 10, "columns": "ABCD", "seat_count": 40},
        {"number": 2, "seat_class": "economy", "rows": 5, "columns": "ABCD", "seat_count": 20},
        {"number": 3, "seat_class": "ac_business", "rows": 4, "columns": "ABC", "seat_count": 12},
    ]


def test_run_detail_shows_cancelled_runs(api, network):
    run = make_run(network.train, tomorrow(), status=TrainRun.Status.CANCELLED)
    response = api.get(f"{RUNS_URL}{run.id}/")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_unknown_run_is_a_json_404(api, network):
    response = api.get(f"{RUNS_URL}999999/")
    assert response.status_code == 404
    assert error_code(response) == "not_found"


def test_absurd_run_ids_never_reach_the_database(api, network):
    assert api.get(f"{RUNS_URL}{'9' * 25}/").status_code == 404


# --- Demo seed ----------------------------------------------------------------------------


def snapshot():
    return (
        Station.objects.count(),
        Train.objects.count(),
        Coach.objects.count(),
        Seat.objects.count(),
        Fare.objects.count(),
        TrainRun.objects.count(),
    )


def test_seed_demo_builds_a_consistent_network_and_is_idempotent():
    seed("--days", "2")
    assert snapshot() == (10, 6, 48, 3120, 18, 12)
    seed("--days", "2")
    assert snapshot() == (10, 6, 48, 3120, 18, 12)


def test_seeded_runs_are_searchable(api):
    seed("--days", "3")
    body = search(api).json()
    assert body["count"] == 1
    offered = {item["seat_class"]: item["seat_count"] for item in body["results"][0]["classes"]}
    assert offered == {"economy": 288, "ac_standard": 192, "ac_business": 40}


@pytest.mark.parametrize("args", [("--days", "0"), ("--days", "121"), ("--booking-window-days", "0")])
def test_seed_demo_rejects_bad_arguments(args):
    with pytest.raises(CommandError):
        seed(*args)
