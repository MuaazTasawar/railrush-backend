"""Public, read-only catalog endpoints.

They need no sign-in, so authentication is switched off: nothing is decoded or looked up per
request, and an Authorization header (or a stale token) can never turn a catalog read into a
401. nginx micro-caches these paths; it never caches a request that carries Authorization.
"""

from django.utils.cache import patch_cache_control
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny

from .models import Station, TrainRun
from .serializers import (
    RunDetailSerializer,
    RunSearchSerializer,
    RunSerializer,
    StationSerializer,
)
from .timeutils import local_day_bounds


class PublicReadMixin:
    authentication_classes = []
    permission_classes = [AllowAny]
    allowed_query_params = frozenset()
    cache_seconds = 1

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        # Unknown parameters are refused before any query runs. This bounds the number of
        # distinct cache keys, so "?x=1", "?x=2", ... cannot be used to bypass the micro-cache.
        if set(request.query_params) - self.allowed_query_params:
            allowed = ", ".join(sorted(self.allowed_query_params)) or "none"
            raise ValidationError({"query": [f"Unsupported query parameter. Allowed: {allowed}."]})

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if response.status_code == 200:
            patch_cache_control(response, public=True, max_age=self.cache_seconds)
        return response


def _runs_with_details():
    return TrainRun.objects.select_related("train__origin", "train__destination").prefetch_related(
        "train__fares", "train__coaches"
    )


class StationListView(PublicReadMixin, ListAPIView):
    """Every station, unpaginated so clients can fill a picker with one request."""

    serializer_class = StationSerializer
    queryset = Station.objects.all()
    pagination_class = None
    cache_seconds = 60


@extend_schema(
    parameters=[
        OpenApiParameter("origin", str, required=True, description="Origin station code, e.g. KHI."),
        OpenApiParameter("destination", str, required=True, description="Destination station code."),
        OpenApiParameter(
            "date",
            OpenApiTypes.DATE,
            required=True,
            description="Local departure date (Asia/Karachi), YYYY-MM-DD.",
        ),
    ]
)
class RunSearchView(PublicReadMixin, ListAPIView):
    serializer_class = RunSerializer
    allowed_query_params = frozenset({"origin", "destination", "date", "page"})

    def get_queryset(self):
        params = RunSearchSerializer(data=self.request.query_params)
        params.is_valid(raise_exception=True)
        query = params.validated_data
        start, end = local_day_bounds(query["date"])
        return (
            _runs_with_details()
            .filter(
                status=TrainRun.Status.SCHEDULED,
                train__origin__code=query["origin"],
                train__destination__code=query["destination"],
                departure_at__gte=start,
                departure_at__lt=end,
            )
            .order_by("departure_at", "id")
        )


class RunDetailView(PublicReadMixin, RetrieveAPIView):
    serializer_class = RunDetailSerializer
    queryset = _runs_with_details()
