import os
import random
import uuid
from types import SimpleNamespace

import pytest
from redis.exceptions import RedisError

from apps.common import fast_throttle
from apps.common.exceptions import DependencyUnavailable
from apps.common.redis import get_redis


class Three(fast_throttle.FastUserRateThrottle):
    rate = "3/min"


class ThreeAnon(fast_throttle.FastAnonRateThrottle):
    rate = "3/min"


@pytest.fixture
def redis_on(monkeypatch):
    """Real Redis, with the fast path forced on. Skips without Redis; never skips in the Docker test service."""
    client = get_redis()
    try:
        client.ping()
    except (RedisError, OSError):
        if os.environ.get("REQUIRE_REDIS_TESTS") == "1":
            pytest.fail("Redis is required for these tests but is not reachable.")
        pytest.skip("Redis is not reachable here; these tests run in the Docker test service.")
    monkeypatch.setattr(fast_throttle, "_cache_is_redis", lambda: True)
    created = []
    yield created
    for key in created:
        client.delete(fast_throttle.KEY_PREFIX + key)


def make_request(pk, authenticated=True, addr="203.0.113.9"):
    return SimpleNamespace(
        user=SimpleNamespace(pk=pk, is_authenticated=authenticated), META={"REMOTE_ADDR": addr}, headers={}
    )


def test_hit_counts_inside_one_window_and_sets_an_expiry(redis_on):
    key = "test_" + uuid.uuid4().hex
    redis_on.append(key)
    results = [fast_throttle.hit(key, 60) for _ in range(3)]
    assert [count for count, _ in results] == [1, 2, 3]
    assert all(0 < ttl <= 60000 for _, ttl in results)


def test_user_throttle_allows_the_limit_then_refuses(redis_on):
    request = make_request(uuid.uuid4())
    outcomes = []
    for _ in range(4):
        throttle = Three()
        outcomes.append(throttle.allow_request(request, None))
        redis_on.append(throttle.key)
    assert outcomes == [True, True, True, False]
    assert 0 < throttle.wait() <= 60


def test_users_are_counted_separately(redis_on):
    first, second = make_request(uuid.uuid4()), make_request(uuid.uuid4())
    for request in (first, second):
        for _ in range(3):
            throttle = Three()
            assert throttle.allow_request(request, None) is True
            redis_on.append(throttle.key)
    throttle = Three()
    assert throttle.allow_request(first, None) is False
    redis_on.append(throttle.key)


def test_anonymous_throttle_counts_per_address_and_skips_signed_in_users(redis_on):
    addr = "198.51.%d.%d" % (random.randint(1, 254), random.randint(1, 254))
    anonymous = make_request(None, authenticated=False, addr=addr)
    outcomes = []
    for _ in range(4):
        throttle = ThreeAnon()
        outcomes.append(throttle.allow_request(anonymous, None))
        redis_on.append(throttle.key)
    assert outcomes == [True, True, True, False]
    signed_in = ThreeAnon()
    assert signed_in.allow_request(make_request(uuid.uuid4()), None) is True
    assert signed_in.key is None


def test_scoped_throttle_uses_the_rate_of_the_views_scope(redis_on):
    view = SimpleNamespace(throttle_scope="hold")  # 30 per minute in the settings
    request = make_request(uuid.uuid4())
    allowed = 0
    for _ in range(32):
        throttle = fast_throttle.FastScopedRateThrottle()
        allowed += int(throttle.allow_request(request, view))
        redis_on.append(throttle.key)
    assert allowed == 30
    assert fast_throttle.FastScopedRateThrottle().allow_request(request, SimpleNamespace()) is True


def test_without_redis_it_behaves_like_the_drf_throttle(redis_on, monkeypatch):
    monkeypatch.setattr(fast_throttle, "_cache_is_redis", lambda: False)
    request = make_request(uuid.uuid4())
    outcomes = [Three().allow_request(request, None) for _ in range(4)]
    assert outcomes == [True, True, True, False]


def test_default_throttle_classes_are_the_fast_ones(settings):
    assert settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] == [
        "apps.common.fast_throttle.FastAnonRateThrottle",
        "apps.common.fast_throttle.FastUserRateThrottle",
        "apps.common.fast_throttle.FastScopedRateThrottle",
    ]


def test_the_setting_switches_the_fast_path_off(settings):
    settings.FAST_THROTTLES = False
    assert fast_throttle._cache_is_redis() is False


def test_a_redis_outage_becomes_a_503(monkeypatch):
    def broken():
        raise RedisError("redis is down")

    monkeypatch.setattr(fast_throttle, "get_redis", broken)
    with pytest.raises(DependencyUnavailable):
        fast_throttle.hit("outage-test", 60)
