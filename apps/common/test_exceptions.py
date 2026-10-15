import logging

import pytest
from django.core.exceptions import PermissionDenied
from django.http import Http404
from redis.exceptions import ConnectionError as RedisConnectionError
from rest_framework import exceptions as drf_exceptions
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from .exceptions import Conflict, DependencyUnavailable

factory = APIRequestFactory()


def make_view(action, authenticated=False):
    class _View(APIView):
        throttle_classes = []
        permission_classes = [IsAuthenticated] if authenticated else [AllowAny]

        def get(self, request):
            return action(request)

        def post(self, request):
            return action(request)

    if not authenticated:
        _View.authentication_classes = []
    return _View.as_view()


def raising(exc):
    def action(request):
        raise exc

    return action


def call(exc, method="get", **kwargs):
    view = make_view(raising(exc))
    return view(getattr(factory, method)("/probe/", **kwargs))


def test_validation_error_uses_envelope_with_details():
    response = call(drf_exceptions.ValidationError({"seat": ["This field is required."]}))
    assert response.status_code == 400
    error = response.data["error"]
    assert error["code"] == "validation_error"
    assert error["details"]["seat"] == ["This field is required."]


def test_unhandled_exception_never_leaks_and_is_logged_with_reference(caplog):
    with caplog.at_level(logging.ERROR, logger="apps.common.exceptions"):
        response = call(RuntimeError("db password is hunter2"))
    assert response.status_code == 500
    error = response.data["error"]
    assert error["code"] == "server_error"
    assert "hunter2" not in str(response.data)
    assert len(error["reference"]) == 12
    assert any(error["reference"] in record.getMessage() for record in caplog.records)
    assert any(record.exc_info for record in caplog.records)  # traceback is in the log, not the response


def test_missing_credentials_return_401_with_challenge_header():
    view = make_view(lambda request: Response({}), authenticated=True)
    response = view(factory.get("/probe/"))
    assert response.status_code == 401
    assert response.data["error"]["code"] == "not_authenticated"
    assert response["WWW-Authenticate"].startswith("Bearer")


def test_invalid_token_response_exposes_no_token_internals():
    view = make_view(lambda request: Response({}), authenticated=True)
    response = view(factory.get("/probe/", HTTP_AUTHORIZATION="Bearer not-a-real-token"))
    assert response.status_code == 401
    assert set(response.data["error"]) <= {"code", "message", "details", "reference"}
    assert "token_class" not in str(response.data)
    assert "messages" not in str(response.data)


def test_django_permission_denied_hides_internal_reason():
    response = call(PermissionDenied("internal reason: user is not the booking owner"))
    assert response.status_code == 403
    assert response.data["error"]["code"] == "permission_denied"
    assert "internal reason" not in str(response.data)


def test_django_404_hides_internal_message():
    response = call(Http404("no row in table bookings_booking"))
    assert response.status_code == 404
    assert response.data["error"]["code"] == "not_found"
    assert "bookings_booking" not in str(response.data)


def test_throttled_reports_retry_after_in_body_and_header():
    response = call(drf_exceptions.Throttled(wait=7))
    assert response.status_code == 429
    assert response.data["error"]["code"] == "throttled"
    assert response.data["error"]["details"] == {"retry_after": 7}
    assert response["Retry-After"] == "7"


def test_malformed_json_does_not_echo_parser_message():
    view = make_view(lambda request: Response(request.data))
    request = factory.post("/probe/", data='{"seat": ', content_type="application/json")
    response = view(request)
    assert response.status_code == 400
    assert response.data["error"]["code"] == "malformed_request"
    assert "JSON parse error" not in str(response.data)
    assert "Expecting" not in str(response.data)


def test_redis_outage_returns_503_without_hostnames():
    response = call(RedisConnectionError("could not reach redis.internal:6379"))
    assert response.status_code == 503
    assert response.data["error"]["code"] == "unavailable"
    assert response["Retry-After"] == "5"
    assert "redis.internal" not in str(response.data)


def test_unsupported_method_returns_405_envelope():
    view = make_view(lambda request: Response({}))
    response = view(factory.put("/probe/"))
    assert response.status_code == 405
    assert response.data["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize(
    ("exc", "status_code", "code"),
    [
        (Conflict(), 409, "conflict"),
        (DependencyUnavailable(), 503, "unavailable"),
        (drf_exceptions.NotFound(), 404, "not_found"),
        (drf_exceptions.UnsupportedMediaType("text/xml"), 415, "unsupported_media_type"),
    ],
)
def test_status_to_code_mapping(exc, status_code, code):
    response = call(exc)
    assert response.status_code == status_code
    assert response.data["error"]["code"] == code
