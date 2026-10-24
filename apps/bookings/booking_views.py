"""Booking and payment endpoints.

  POST /api/bookings/                      turn my live seat holds into a booking (Idempotency-Key header)
  GET  /api/bookings/                      my bookings
  GET  /api/bookings/<id>/                 one of my bookings
  POST /api/bookings/<id>/pay/             start (or reuse) a payment
  POST /api/bookings/<id>/cancel/          cancel an unpaid booking
  POST /api/payments/webhook/              gateway callback, authenticated by an HMAC signature

Other people's bookings answer 404, the same as a booking that does not exist.
"""

import json
import logging
import re

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import generics, serializers, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common import fast_throttle

from . import booking_services as services
from . import gateway
from .booking_serializers import (
    BookingCreateSerializer,
    BookingSerializer,
    PaymentSerializer,
)
from .models import Booking

logger = logging.getLogger(__name__)

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_WEBHOOK_BYTES = 16384


class BookingWriteThrottle(fast_throttle.FastUserRateThrottle):
    scope = "booking"

    def get_rate(self):
        return getattr(settings, "THROTTLE_BOOKING", "20/min")


class WebhookThrottle(fast_throttle.FixedWindowThrottle):
    scope = "payment_webhook"

    def get_rate(self):
        return getattr(settings, "THROTTLE_PAYMENT_WEBHOOK", "600/min")

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class _WriteThrottleMixin:
    def get_throttles(self):
        throttles = super().get_throttles()
        if getattr(getattr(self, "request", None), "method", None) == "POST":
            throttles.append(BookingWriteThrottle())
        return throttles


def _idempotency_key(request):
    key = request.META.get("HTTP_IDEMPOTENCY_KEY", "")
    if not KEY_RE.match(key):
        raise serializers.ValidationError(
            {
                "idempotency_key": [
                    "Send an Idempotency-Key header of 8 to 64 letters, digits, dashes or underscores."
                ]
            }
        )
    return key


def _error(code, message, http_status):
    return Response(
        {"error": {"code": code, "message": message, "details": {}}}, status=http_status
    )


@method_decorator(never_cache, name="dispatch")
class BookingsView(_WriteThrottleMixin, generics.GenericAPIView):
    serializer_class = BookingSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Booking.objects.none()
        return Booking.objects.filter(user_id=self.request.user.id).prefetch_related("seats")

    @extend_schema(responses=BookingSerializer(many=True))
    def get(self, request):
        queryset = self.get_queryset()
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(queryset, many=True).data)

    @extend_schema(
        request=BookingCreateSerializer,
        responses={200: BookingSerializer, 201: BookingSerializer, 409: OpenApiTypes.OBJECT},
    )
    def post(self, request):
        key = _idempotency_key(request)
        body = BookingCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        booking, created = services.create_booking(
            request.user, body.validated_data["run_id"], key
        )
        code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(BookingSerializer(booking).data, status=code)


@method_decorator(never_cache, name="dispatch")
class BookingDetailView(APIView):
    @extend_schema(responses=BookingSerializer)
    def get(self, request, booking_id):
        booking = (
            Booking.objects.filter(pk=booking_id, user_id=request.user.id).prefetch_related("seats").first()
        )
        if booking is None:
            raise NotFound("Booking not found.")
        return Response(BookingSerializer(booking).data)


@method_decorator(never_cache, name="dispatch")
class PayView(_WriteThrottleMixin, APIView):
    @extend_schema(request=None, responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT})
    def post(self, request, booking_id):
        payment, created = services.start_payment(request.user, booking_id)
        body = {
            "booking_id": str(booking_id),
            "payment": PaymentSerializer(payment).data,
            "checkout": gateway.get_gateway().checkout(payment),
        }
        return Response(body, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@method_decorator(never_cache, name="dispatch")
class BookingCancelView(_WriteThrottleMixin, APIView):
    @extend_schema(request=None, responses=BookingSerializer)
    def post(self, request, booking_id):
        booking = services.cancel_booking(request.user, booking_id)
        return Response(BookingSerializer(booking).data)


@method_decorator(never_cache, name="dispatch")
class PaymentWebhookView(APIView):
    """Called by the gateway, not by a signed-in user. The signature is the only credential."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [WebhookThrottle]

    @extend_schema(exclude=True)
    def post(self, request):
        if not settings.PAYMENT_WEBHOOK_SECRET:
            logger.error("payments.webhook_secret_missing")
            return _error(
                "webhook_unavailable", "Payment webhooks are not configured.", status.HTTP_503_SERVICE_UNAVAILABLE
            )
        raw = request.body
        signature = request.META.get(gateway.SIGNATURE_META_KEY, "")
        if len(raw) > MAX_WEBHOOK_BYTES or not gateway.signature_is_valid(raw, signature):
            return _error("invalid_signature", "Signature check failed.", status.HTTP_400_BAD_REQUEST)
        try:
            event = json.loads(raw)
            ref = event["provider_ref"]
            outcome = event["status"]
            amount = event["amount_pkr"]
        except (ValueError, KeyError, TypeError):
            return _error("invalid_payload", "Malformed event.", status.HTTP_400_BAD_REQUEST)
        if (
            not isinstance(ref, str)
            or not 1 <= len(ref) <= 64
            or outcome not in ("succeeded", "failed")
            or type(amount) is not int
            or amount < 1
        ):
            return _error("invalid_payload", "Malformed event.", status.HTTP_400_BAD_REQUEST)
        result = services.apply_payment_event(
            ref, outcome == "succeeded", amount, {"provider_ref": ref, "status": outcome, "amount_pkr": amount}
        )
        if result == "amount_mismatch":
            return _error(
                "amount_mismatch", "The amount does not match the payment.", status.HTTP_400_BAD_REQUEST
            )
        return Response({"result": result})
