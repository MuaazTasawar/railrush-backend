import os
import time
import uuid
from datetime import datetime, timezone

import pytest
from django.core.cache import cache
from redis.exceptions import RedisError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.catalog.models import Station, Train, TrainRun
from apps.common.redis import get_redis, reset_redis_client

from . import services


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def redis_client(settings):
    """A real Redis with a private key prefix. Skips on a PC without Redis; never skips in Docker."""
    reset_redis_client()
    client = get_redis()
    try:
        client.ping()
    except (RedisError, OSError) as exc:
        if os.environ.get("REQUIRE_REDIS_TESTS") == "1":
            pytest.fail(f"Redis is required for these tests but is not reachable ({type(exc).__name__}).")
        pytest.skip("Redis is not reachable here; these tests run in the Docker test service.")
    prefix = f"wrtest-{uuid.uuid4().hex[:10]}"
    settings.WAITROOM_KEY_PREFIX = prefix
    yield client
    for key in client.scan_iter(match=f"{prefix}:*", count=500):
        client.delete(key)
    reset_redis_client()


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock(time.time())
    monkeypatch.setattr(services, "now", fake)
    return fake


def _utc(timestamp):
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


@pytest.fixture
def make_run(db, clock):
    khi = Station.objects.create(code="KHI", name="Karachi Cantonment", city="Karachi")
    lhe = Station.objects.create(code="LHE", name="Lahore Junction", city="Lahore")
    train = Train.objects.create(number="101", name="Eid Express", origin=khi, destination=lhe)
    made = []

    def factory(opens_in=-3600, departs_in=3 * 86400, status=TrainRun.Status.SCHEDULED):
        departs = clock.t + departs_in + len(made)
        run = TrainRun.objects.create(
            train=train,
            departure_at=_utc(departs),
            arrival_at=_utc(departs + 18 * 3600),
            booking_opens_at=_utc(clock.t + opens_in),
            status=status,
        )
        made.append(run)
        return run

    return factory


@pytest.fixture
def make_user(db):
    def factory(number):
        return User.objects.create_user(email=f"rider{number}@example.com")

    return factory


@pytest.fixture
def auth():
    def headers(user):
        token = RefreshToken.for_user(user).access_token
        return {"HTTP_AUTHORIZATION": f"Bearer {token}"}

    return headers
