"""Waiting-room endpoints.

  GET  /api/waitroom/<run_id>/status/  public; nginx caches it for one second
  POST /api/waitroom/<run_id>/join/    signed in; take a place in the queue (idempotent)
  GET  /api/waitroom/<run_id>/me/      signed in; my place, and my pass once admitted
"""

from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.views import PublicReadMixin
from apps.common.exceptions import Conflict

from . import services
from .schemas import PublicStatusSerializer, TicketStatusSerializer

RUN_ID = OpenApiParameter(
    "run_id", OpenApiTypes.INT, OpenApiParameter.PATH, description="Catalog run id."
)


class StatusView(PublicReadMixin, APIView):
    """Queue-wide numbers for a run. Anonymous and identical for everyone, so it can be cached."""

    @extend_schema(parameters=[RUN_ID], responses=PublicStatusSerializer)
    def get(self, request, run_id):
        info = services.get_run_info(int(run_id))
        return Response(services.public_status(info, services.now()))


@method_decorator(never_cache, name="dispatch")
class JoinView(APIView):
    throttle_scope = "waitroom_join"

    @extend_schema(
        parameters=[RUN_ID],
        request=None,
        responses={200: TicketStatusSerializer, 201: TicketStatusSerializer},
    )
    def post(self, request, run_id):
        info = services.get_run_info(int(run_id))
        state = services.room_state(info, services.now())
        if state == services.CLOSED:
            raise Conflict("The waiting room for this run is closed.")
        if state == services.OPENING_SOON:
            raise Conflict("The waiting room is not open yet.")
        ticket, created = services.join(info, request.user.id)
        body = services.ticket_status(info, request.user.id, ticket, services.now())
        return Response(body, status=201 if created else 200)


@method_decorator(never_cache, name="dispatch")
class MeView(APIView):
    throttle_scope = "waitroom_me"

    @extend_schema(parameters=[RUN_ID], responses=TicketStatusSerializer)
    def get(self, request, run_id):
        info = services.get_run_info(int(run_id))
        ticket = services.get_ticket(info, request.user.id)
        if ticket is None:
            raise NotFound("You are not in the queue for this run.")
        return Response(services.ticket_status(info, request.user.id, ticket, services.now()))
