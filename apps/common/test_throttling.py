import pytest
from django.core.cache import cache
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from .throttling import LoginAccountThrottle, LoginIPThrottle, RegisterThrottle

factory = APIRequestFactory()


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def make_request(email=None, ip="10.0.0.1", **extra):
    payload = {} if email is None else {"email": email}
    raw = factory.post("/api/auth/login/", payload, format="json", REMOTE_ADDR=ip, **extra)
    return Request(raw, parsers=[JSONParser()])


def allowed(throttle_class, request):
    # DRF builds a fresh throttle instance per request; do the same here.
    return throttle_class().allow_request(request, None)


def test_login_ip_throttle_blocks_after_limit():
    limit = LoginIPThrottle().num_requests
    request = make_request()
    assert all(allowed(LoginIPThrottle, request) for _ in range(limit))
    assert allowed(LoginIPThrottle, request) is False


def test_login_ip_throttle_is_per_client():
    limit = LoginIPThrottle().num_requests
    for _ in range(limit):
        allowed(LoginIPThrottle, make_request(ip="10.0.0.1"))
    assert allowed(LoginIPThrottle, make_request(ip="10.0.0.1")) is False
    assert allowed(LoginIPThrottle, make_request(ip="10.0.0.2")) is True


def test_register_throttle_blocks_after_limit():
    limit = RegisterThrottle().num_requests
    request = make_request()
    assert all(allowed(RegisterThrottle, request) for _ in range(limit))
    assert allowed(RegisterThrottle, request) is False


def test_forged_forwarded_for_is_ignored_without_trusted_proxy(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 0}
    limit = LoginIPThrottle().num_requests
    for i in range(limit):
        allowed(LoginIPThrottle, make_request(ip="10.0.0.1", HTTP_X_FORWARDED_FOR=f"203.0.113.{i}"))
    forged = make_request(ip="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.9")
    assert allowed(LoginIPThrottle, forged) is False


def test_trusted_proxy_uses_rightmost_forwarded_for_entry(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    limit = LoginIPThrottle().num_requests
    for i in range(limit):
        # The client forges the left-most entry; the proxy appends the real address last.
        header = f"203.0.113.{i}, 198.51.100.7"
        allowed(LoginIPThrottle, make_request(ip="172.18.0.2", HTTP_X_FORWARDED_FOR=header))
    same_client = make_request(ip="172.18.0.2", HTTP_X_FORWARDED_FOR="203.0.113.250, 198.51.100.7")
    other_client = make_request(ip="172.18.0.2", HTTP_X_FORWARDED_FOR="203.0.113.250, 198.51.100.8")
    assert allowed(LoginIPThrottle, same_client) is False
    assert allowed(LoginIPThrottle, other_client) is True


def test_account_throttle_key_never_contains_the_email():
    key = LoginAccountThrottle().get_cache_key(make_request("Victim@Example.com"), None)
    assert "victim" not in key.lower()
    assert "example" not in key.lower()


def test_account_throttle_normalizes_case_and_whitespace():
    throttle = LoginAccountThrottle()
    first = throttle.get_cache_key(make_request("Victim@Example.com"), None)
    second = throttle.get_cache_key(make_request("  victim@example.COM "), None)
    assert first == second


def test_account_throttle_blocks_across_different_ips():
    limit = LoginAccountThrottle().num_requests
    for i in range(limit):
        assert allowed(LoginAccountThrottle, make_request("victim@example.com", ip=f"10.0.1.{i}")) is True
    assert allowed(LoginAccountThrottle, make_request("victim@example.com", ip="10.0.2.1")) is False
    assert allowed(LoginAccountThrottle, make_request("other@example.com", ip="10.0.2.1")) is True


def test_account_throttle_skips_requests_without_an_email():
    request = make_request()
    assert LoginAccountThrottle().get_cache_key(request, None) is None
    assert allowed(LoginAccountThrottle, request) is True


def test_account_throttle_ignores_non_object_json():
    raw = factory.post("/api/auth/login/", ["not", "an", "object"], format="json")
    request = Request(raw, parsers=[JSONParser()])
    assert LoginAccountThrottle().get_cache_key(request, None) is None
