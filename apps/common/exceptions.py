"""One consistent, non-leaky error envelope for the whole API.

Every error response has this shape:

    {"error": {"code": "...", "message": "...", "details": ..., "reference": "..."}}

`details` appears only for validation errors and throttling. `reference` appears only for
server-side failures: it also goes into the server log, so a user can quote it to support
without the response ever exposing a traceback, hostname, SQL or library message.
"""

import logging
import uuid

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from redis.exceptions import RedisError
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class Conflict(drf_exceptions.APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


class DependencyUnavailable(drf_exceptions.APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Service temporarily unavailable. Please retry shortly."
    default_code = "unavailable"


_STATUS_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    415: "unsupported_media_type",
    429: "throttled",
    503: "unavailable",
}


def _envelope(code, message, details=None, reference=None):
    body = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    if reference is not None:
        body["reference"] = reference
    return {"error": body}


def _reference():
    return uuid.uuid4().hex[:12]


def _view_name(context):
    view = context.get("view")
    return view.__class__.__name__ if view is not None else "unknown"


def exception_handler(exc, context):
    # Redis outage (throttle cache, seat holds, waiting room): tell clients to retry, leak nothing.
    if isinstance(exc, RedisError):
        ref = _reference()
        logger.warning(
            "Redis unavailable (ref=%s, view=%s, error=%s)",
            ref,
            _view_name(context),
            exc.__class__.__name__,
        )
        response = Response(
            _envelope(
                "unavailable",
                DependencyUnavailable.default_detail,
                reference=ref,
            ),
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        response["Retry-After"] = "5"
        return response

    response = drf_exception_handler(exc, context)

    # Not an API exception: a bug. Log everything server-side, tell the client nothing.
    if response is None:
        ref = _reference()
        logger.error(
            "Unhandled exception (ref=%s, view=%s)",
            ref,
            _view_name(context),
            exc_info=exc,
        )
        return Response(
            _envelope("server_error", "Something went wrong on our side.", reference=ref),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    details = None
    if isinstance(exc, drf_exceptions.ValidationError):
        code = "validation_error"
        message = "The request data is invalid."
        details = response.data
    elif isinstance(exc, drf_exceptions.ParseError):
        # The raw parser message can echo request content, so it is replaced.
        code = "malformed_request"
        message = "Malformed request body."
    elif isinstance(exc, drf_exceptions.Throttled):
        code = "throttled"
        message = "Too many requests. Please slow down."
        if exc.wait is not None:
            details = {"retry_after": int(exc.wait)}
    elif isinstance(exc, Http404):
        # Django's message can name models or tables ("No Booking matches the given query").
        code = "not_found"
        message = "Not found."
    elif isinstance(exc, DjangoPermissionDenied):
        code = "permission_denied"
        message = "You do not have permission to perform this action."
    else:
        code = _STATUS_CODES.get(response.status_code, "error")
        data = response.data
        detail = data.get("detail") if isinstance(data, dict) else None
        message = str(detail) if detail else "The request could not be processed."

    response.data = _envelope(code, message, details=details)
    return response
