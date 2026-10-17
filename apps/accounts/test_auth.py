"""Accounts API tests: registration, login, refresh, profile, and the abuse cases."""

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import pytest
from django.db import IntegrityError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.common.throttling import (
    LoginAccountThrottle,
    LoginIPThrottle,
    RefreshThrottle,
    RegisterThrottle,
)

from .models import User, UserManager

pytestmark = pytest.mark.django_db

REGISTER = "/api/auth/register/"
LOGIN = "/api/auth/login/"
REFRESH = "/api/auth/refresh/"
ME = "/api/users/me/"

GOOD_PW = "Zebra-Quartz-Lantern-42"
WRONG_PW = "Wrong-Horse-Staple-77"
LONG_PW = "x" * 129
LONG_NAME = "n" * 151
PBKDF2_ONLY = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]


def make_user(email="rider@example.com", pw=GOOD_PW, **extra):
    return User.objects.create_user(email=email, password=pw, **extra)


def tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


def bearer(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


def error_code(response):
    return response.json()["error"]["code"]


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge(user_id, alg="HS256", key=""):
    """Build a token the server never issued: unsigned (alg none) or signed with the wrong key."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": alg, "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps(
            {
                "token_type": "access",
                "exp": now + 600,
                "iat": now,
                "jti": uuid.uuid4().hex,
                "user_id": str(user_id),
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}"
    if alg == "none":
        return signing_input + "."
    signature = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


# --- Registration -----------------------------------------------------------


def test_register_returns_user_and_tokens(api):
    response = api.post(
        REGISTER, {"email": "  Rider@Example.COM ", "password": GOOD_PW, "full_name": "Rider One"}
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user", "access", "refresh"}
    assert body["user"]["email"] == "rider@example.com"
    assert body["user"]["full_name"] == "Rider One"
    assert "password" not in body["user"]
    assert GOOD_PW not in response.content.decode()
    assert AccessToken(body["access"])["user_id"] == body["user"]["id"]
    assert "no-store" in response["Cache-Control"]
    assert User.objects.get(email="rider@example.com").check_password(GOOD_PW)


def test_register_ignores_privileged_fields(api):
    payload = {
        "email": "sneaky@example.com",
        "password": GOOD_PW,
        "is_staff": True,
        "is_superuser": True,
        "is_active": False,
        "id": str(uuid.uuid4()),
    }
    response = api.post(REGISTER, payload)
    assert response.status_code == 201
    user = User.objects.get(email="sneaky@example.com")
    assert (user.is_staff, user.is_superuser, user.is_active) == (False, False, True)
    assert str(user.id) != payload["id"]


def test_register_duplicate_email_is_a_conflict_regardless_of_case(api):
    make_user("dup@example.com")
    response = api.post(REGISTER, {"email": "DUP@example.com", "password": GOOD_PW})
    assert response.status_code == 409
    assert error_code(response) == "conflict"
    assert User.objects.count() == 1


def test_register_race_on_unique_constraint_is_a_conflict(api, monkeypatch):
    def boom(self, *args, **kwargs):
        raise IntegrityError("duplicate key value violates unique constraint users_email_key")

    monkeypatch.setattr(UserManager, "create_user", boom)
    response = api.post(REGISTER, {"email": "race@example.com", "password": GOOD_PW})
    assert response.status_code == 409
    assert "constraint" not in response.content.decode()


@pytest.mark.parametrize("candidate", ["short1A!", "qwertyuiop", "1234567890", "weak@example.com"])
def test_register_rejects_weak_passwords(api, candidate):
    response = api.post(REGISTER, {"email": "weak@example.com", "password": candidate})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "password" in error["details"]
    assert candidate not in response.content.decode().replace("weak@example.com", "")
    assert not User.objects.filter(email="weak@example.com").exists()


BAD_REGISTRATIONS = [
    {},
    {"email": "not-an-email", "password": GOOD_PW},
    {"email": "a@example.com"},
    {"email": "a@example.com", "password": LONG_PW},
    {"email": "a@example.com", "password": GOOD_PW, "full_name": LONG_NAME},
    {"email": "a@example.com", "password": GOOD_PW, "full_name": "bad\x00name"},
    {"email": ["a@example.com"], "password": GOOD_PW},
]


@pytest.mark.parametrize("payload", BAD_REGISTRATIONS)
def test_register_rejects_malformed_input_with_400_never_500(api, payload):
    response = api.post(REGISTER, payload)
    assert response.status_code == 400
    assert error_code(response) == "validation_error"
    assert User.objects.count() == 0


def test_register_rejects_a_non_object_body(api):
    response = api.post(REGISTER, ["not", "an", "object"], format="json")
    assert response.status_code == 400


def test_password_is_stored_hashed(api, settings):
    settings.PASSWORD_HASHERS = PBKDF2_ONLY
    api.post(REGISTER, {"email": "hash@example.com", "password": GOOD_PW})
    stored = User.objects.get(email="hash@example.com").password
    assert stored.startswith("pbkdf2_sha256$")
    assert GOOD_PW not in stored


# --- Login ------------------------------------------------------------------


def test_login_returns_user_and_tokens(api):
    user = make_user()
    response = api.post(LOGIN, {"email": "rider@example.com", "password": GOOD_PW})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"user", "access", "refresh"}
    claims = AccessToken(body["access"]).payload
    assert claims["user_id"] == str(user.id)
    assert not {"email", "password"} & set(claims)
    assert "no-store" in response["Cache-Control"]


def test_login_normalizes_email(api):
    make_user()
    response = api.post(LOGIN, {"email": "  RIDER@Example.com ", "password": GOOD_PW})
    assert response.status_code == 200


def test_login_failures_are_indistinguishable(api):
    make_user()
    make_user("off@example.com", is_active=False)
    wrong = api.post(LOGIN, {"email": "rider@example.com", "password": WRONG_PW})
    unknown = api.post(LOGIN, {"email": "ghost@example.com", "password": WRONG_PW})
    inactive = api.post(LOGIN, {"email": "off@example.com", "password": GOOD_PW})
    assert wrong.status_code == unknown.status_code == inactive.status_code == 401
    assert wrong.json() == unknown.json() == inactive.json()
    assert error_code(wrong) == "not_authenticated"


BAD_LOGINS = [
    {},
    {"email": "a@example.com"},
    {"password": GOOD_PW},
    {"email": ["a@example.com"], "password": GOOD_PW},
    {"email": "a@example.com", "password": LONG_PW},
    {"email": "a\x00@example.com", "password": GOOD_PW},
]


@pytest.mark.parametrize("payload", BAD_LOGINS)
def test_login_rejects_malformed_input_with_400(api, payload):
    assert api.post(LOGIN, payload).status_code == 400


def test_login_treats_injection_strings_as_a_failed_login(api):
    make_user()
    response = api.post(LOGIN, {"email": "' OR 1=1 --", "password": GOOD_PW})
    assert response.status_code == 401


def test_login_ip_throttle_trips_and_reports_retry_after(api):
    limit = LoginIPThrottle().num_requests
    for i in range(limit):
        response = api.post(
            LOGIN, {"email": f"user{i}@example.com", "password": WRONG_PW}, REMOTE_ADDR="10.20.0.1"
        )
        assert response.status_code == 401
    blocked = api.post(
        LOGIN, {"email": "another@example.com", "password": WRONG_PW}, REMOTE_ADDR="10.20.0.1"
    )
    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["code"] == "throttled"
    assert error["details"]["retry_after"] > 0
    other_ip = api.post(
        LOGIN, {"email": "another@example.com", "password": WRONG_PW}, REMOTE_ADDR="10.20.0.2"
    )
    assert other_ip.status_code == 401


def test_login_account_throttle_spans_ips_and_blocks_the_right_password_too(api):
    make_user()
    limit = LoginAccountThrottle().num_requests
    for i in range(limit):
        response = api.post(
            LOGIN, {"email": "rider@example.com", "password": WRONG_PW}, REMOTE_ADDR=f"10.30.0.{i}"
        )
        assert response.status_code == 401
    blocked = api.post(
        LOGIN, {"email": "rider@example.com", "password": GOOD_PW}, REMOTE_ADDR="10.30.1.1"
    )
    assert blocked.status_code == 429


def test_register_throttle_trips(api):
    limit = RegisterThrottle().num_requests
    for _ in range(limit):
        assert api.post(REGISTER, {}).status_code == 400
    assert api.post(REGISTER, {}).status_code == 429


# --- Refresh ----------------------------------------------------------------


def test_refresh_issues_a_working_access_token(api):
    user = make_user()
    _, refresh = tokens_for(user)
    response = api.post(REFRESH, {"refresh": refresh})
    assert response.status_code == 200
    assert set(response.json()) == {"access"}
    assert "no-store" in response["Cache-Control"]
    me = api.get(ME, **bearer(response.json()["access"]))
    assert me.status_code == 200


def test_refresh_rejects_access_tokens_and_garbage(api):
    user = make_user()
    access, _ = tokens_for(user)
    assert api.post(REFRESH, {"refresh": access}).status_code == 401
    assert api.post(REFRESH, {"refresh": "not.a.jwt"}).status_code == 401
    assert api.post(REFRESH, {}).status_code == 400


def test_refresh_rejects_expired_and_forged_tokens(api):
    user = make_user()
    expired = RefreshToken.for_user(user)
    expired.set_exp(lifetime=timedelta(seconds=-5))
    assert api.post(REFRESH, {"refresh": str(expired)}).status_code == 401
    forged = forge(user.id, key="not-the-server-signing-key-" * 3)
    assert api.post(REFRESH, {"refresh": forged}).status_code == 401


def test_refresh_refuses_deactivated_and_deleted_accounts(api):
    user = make_user()
    _, refresh = tokens_for(user)
    user.is_active = False
    user.save()
    assert api.post(REFRESH, {"refresh": refresh}).status_code == 401
    user.delete()
    assert api.post(REFRESH, {"refresh": refresh}).status_code == 401


def test_refresh_throttle_trips(api):
    limit = RefreshThrottle().num_requests
    for _ in range(limit):
        assert api.post(REFRESH, {"refresh": "x"}).status_code == 401
    assert api.post(REFRESH, {"refresh": "x"}).status_code == 429


# --- Profile ----------------------------------------------------------------


def test_me_requires_authentication(api):
    response = api.get(ME)
    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"
    assert "Bearer" in response["WWW-Authenticate"]


def test_me_returns_only_public_profile_fields(api):
    user = make_user(full_name="Rider One")
    access, _ = tokens_for(user)
    response = api.get(ME, **bearer(access))
    assert response.status_code == 200
    assert set(response.json()) == {"id", "email", "full_name", "date_joined"}
    assert "no-store" in response["Cache-Control"]


def test_me_patch_changes_the_name_and_nothing_else(api):
    user = make_user()
    access, _ = tokens_for(user)
    response = api.patch(
        ME,
        {
            "full_name": "New Name",
            "email": "hijack@example.com",
            "is_staff": True,
            "id": str(uuid.uuid4()),
        },
        **bearer(access),
    )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.full_name == "New Name"
    assert user.email == "rider@example.com"
    assert user.is_staff is False


@pytest.mark.parametrize("method", ["put", "delete", "post"])
def test_me_does_not_offer_other_methods(api, method):
    user = make_user()
    access, _ = tokens_for(user)
    response = getattr(api, method)(ME, {}, **bearer(access))
    assert response.status_code == 405


def test_me_rejects_a_deactivated_account_even_with_a_valid_token(api):
    user = make_user()
    access, _ = tokens_for(user)
    user.is_active = False
    user.save()
    assert api.get(ME, **bearer(access)).status_code == 401


def test_me_rejects_forged_expired_and_wrong_type_tokens(api):
    user = make_user()
    access, refresh = tokens_for(user)
    expired = AccessToken.for_user(user)
    expired.set_exp(lifetime=timedelta(seconds=-5))
    tampered = access[:-4] + ("AAAA" if not access.endswith("AAAA") else "BBBB")
    candidates = {
        "refresh token as access token": refresh,
        "unsigned token (alg none)": forge(user.id, alg="none"),
        "token signed with another key": forge(user.id, key="not-the-server-signing-key-" * 3),
        "tampered signature": tampered,
        "expired token": str(expired),
        "garbage": "not.a.jwt",
    }
    for label, token in candidates.items():
        assert api.get(ME, **bearer(token)).status_code == 401, label


def test_me_rejects_the_wrong_auth_scheme(api):
    user = make_user()
    access, _ = tokens_for(user)
    response = api.get(ME, HTTP_AUTHORIZATION=f"Token {access}")
    assert response.status_code == 401
