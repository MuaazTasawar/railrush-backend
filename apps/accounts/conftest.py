import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _fast_hashing(settings):
    # PBKDF2 is deliberately slow; most tests only need a working hasher.
    settings.PASSWORD_HASHERS = FAST_HASHERS


@pytest.fixture(autouse=True)
def _clean_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()
