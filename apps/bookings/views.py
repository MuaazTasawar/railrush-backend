"""Seat-hold endpoints.

  GET  /api/catalog/runs/<run_id>/seats/  public seat map, cached by nginx for a moment
  GET  /api/holds/<run_id>/               my live holds (sign in)
  POST /api/holds/<run_id>/               hold seats (sign in and a waiting-room pass)
  POST /api/holds/<run_id>/release/       release some or all of my holds (sign in)
"""

from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.views import PublicReadMixin
from apps.common.exceptions import Conflict
from apps.waitroom.permissions import HasWaitingRoomPass
from apps.waitroom.services import iso

from . import holds, seatmap
from .serializers import (
    HoldRequestSerializer,
    HoldsSerializer,
    ReleasedSerializer,
    ReleaseRequestSerializer,
    SeatMapSerializer,
)

RUN_ID = OpenApiParameter(
    "run_id", OpenApiTypes.INT, OpenApiParameter.PATH, description="Catalog run id."
)


def _holds_body(run_id, seats, at):
    return {
        "run_id": run_id,
        "hold_seconds": settings.HOLD_SECONDS,
        "server_time": iso(at),
        "seats": [{"seat": seat, "expires_at": iso(expires)} for seat, expires in seats.items()],
    }


def _conflict(code, message, details):
    body = {"error": {"code": code, "message": message, "details": details}}
    return Response(body, status=status.HTTP_409_CONFLICT)


class SeatMapView(PublicReadMixin, APIView):
    """Which seats are taken on a run. Public and the same for everyone, so it can be cached."""

    @extend_schema(parameters=[RUN_ID], responses=SeatMapSerializer)
    def get(self, request, run_id):
        layout = seatmap.get_layout(int(run_id))
        return Response(seatmap.build_seatmap(layout, holds.now()))


@method_decorator(never_cache, name="dispatch")
class HoldsView(APIView):
    def get_permissions(self):
        method = getattr(getattr(self, "request", None), "method", None)
        if method == "POST":
            return [IsAuthenticated(), HasWaitingRoomPass()]
        return [IsAuthenticated()]

    @property
    def throttle_scope(self):
        method = getattr(getattr(self, "request", None), "method", None)
        return "hold" if method == "POST" else None

    @extend_schema(parameters=[RUN_ID], responses=HoldsSerializer)
    def get(self, request, run_id):
        run_id = int(run_id)
        at = holds.now()
        return Response(_holds_body(run_id, holds.holds_for_user(run_id, request.user.id, at), at))

    @extend_schema(
        parameters=[RUN_ID],
        request=HoldRequestSerializer,
        responses={200: HoldsSerializer, 201: HoldsSerializer, 409: OpenApiTypes.OBJECT},
    )
    def post(self, request, run_id):
        run_id = int(run_id)
        serializer = HoldRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        layout = seatmap.get_layout(run_id)
        at = holds.now()
        if layout.cancelled or at >= layout.departs_at:
            raise Conflict("Booking is closed for this run.")
        if at < layout.opens_at:
            raise Conflict("Booking has not opened for this run yet.")
        seats = seatmap.validate_labels(layout, serializer.validated_data["seats"])

        sold = seatmap.booked_labels(run_id)
        blocked = [seat for seat in seats if seat in sold]
        if not blocked:
            result = holds.hold(run_id, request.user.id, seats, at)
            if result.status == holds.UNAVAILABLE:
                blocked = [seat for seat in seats if seat in set(result.unavailable)]
            elif result.status == holds.LIMIT:
                return _conflict(
                    "hold_limit_reached",
                    "You are already holding the most seats allowed for this run.",
                    {"max_seats": settings.HOLD_MAX_SEATS_PER_USER},
                )
        if blocked:
            return _conflict(
                "seats_unavailable", "Some of those seats are no longer available.", {"unavailable": blocked}
            )
        code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
        return Response(_holds_body(run_id, result.seats, at), status=code)


@method_decorator(never_cache, name="dispatch")
class ReleaseView(APIView):
    throttle_scope = "hold"

    @extend_schema(
        parameters=[RUN_ID], request=ReleaseRequestSerializer, responses=ReleasedSerializer
    )
    def post(self, request, run_id):
        run_id = int(run_id)
        serializer = ReleaseRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        seats = serializer.validated_data.get("seats") or []
        labels = [seatmap.canonical_label(seat) for seat in seats]
        at = holds.now()
        released = holds.release(run_id, request.user.id, labels, at)
        return Response({"run_id": run_id, "released": released, "server_time": iso(at)})
