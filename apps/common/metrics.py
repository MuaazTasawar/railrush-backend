"""Prometheus metrics for the web tier.

django-prometheus already counts requests, responses and latency per view. This module adds:
  * a counter for payment webhook results (a 200 hides whether the event confirmed, refunded or was a duplicate), and
  * gauges read at scrape time from PostgreSQL and Redis: bookings by status, unpaid bookings past their deadline,
    payments by status, and the waiting-room and hold sizes of the runs that are about to open or have just opened.

Under gunicorn there are several worker processes, so counters live in files under PROMETHEUS_MULTIPROC_DIR and are
combined here. The endpoint is for the internal network only: anything that came through nginx carries an
X-Forwarded-For header and is refused.
"""

import logging
import os
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.http import Http404, HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger(__name__)

OVERDUE_GRACE_SECONDS = 120
MAX_RUNS = 20
QUEUE_WINDOW_BEFORE = timedelta(hours=6)
QUEUE_WINDOW_AFTER = timedelta(hours=24)

PAYMENT_EVENTS = Counter(
    "railrush_payment_events_total",
    "Payment gateway events applied, by result.",
    ["result"],
)


class StateCollector:
    """Gauges read from PostgreSQL and Redis while Prometheus scrapes. A failing source is reported, never raised."""

    def collect(self):
        scrape_ok = GaugeMetricFamily(
            "railrush_scrape_ok", "1 when the source could be read during this scrape.", labels=["source"]
        )
        families = []
        runs = []
        try:
            db_families, runs = self._database()
            families += db_families
            scrape_ok.add_metric(["postgres"], 1)
        except Exception:
            logger.exception("metrics: PostgreSQL could not be read")
            scrape_ok.add_metric(["postgres"], 0)
        try:
            families += self._queues(runs)
            scrape_ok.add_metric(["redis"], 1)
        except Exception:
            logger.exception("metrics: Redis could not be read")
            scrape_ok.add_metric(["redis"], 0)
        yield scrape_ok
        yield from families

    def _database(self):
        # Imported here so apps.common has no import-time ties to the feature apps.
        from django.db.models import Count

        from apps.bookings.models import Booking, Payment
        from apps.catalog.models import TrainRun
        from apps.waitroom import services as waitroom

        now = datetime.fromtimestamp(waitroom.now(), tz=dt_timezone.utc)

        bookings = GaugeMetricFamily("railrush_bookings", "Bookings by status.", labels=["status"])
        counts = dict(Booking.objects.values_list("status").annotate(n=Count("pk")))
        for status in Booking.Status.values:
            bookings.add_metric([status], counts.get(status, 0))

        overdue = Booking.objects.filter(
            status=Booking.Status.PENDING_PAYMENT,
            expires_at__lt=now - timedelta(seconds=OVERDUE_GRACE_SECONDS),
        ).count()
        overdue_family = GaugeMetricFamily(
            "railrush_pending_bookings_overdue",
            "Unpaid bookings more than 2 minutes past their deadline. Above zero for long means expiry (Celery beat) is not running.",
            value=overdue,
        )

        payments = GaugeMetricFamily("railrush_payments", "Payments by status.", labels=["status"])
        payment_counts = dict(Payment.objects.values_list("status").annotate(n=Count("pk")))
        for status in Payment.Status.values:
            payments.add_metric([status], payment_counts.get(status, 0))

        runs = list(
            TrainRun.objects.filter(
                status=TrainRun.Status.SCHEDULED,
                booking_opens_at__gte=now - QUEUE_WINDOW_BEFORE,
                booking_opens_at__lte=now + QUEUE_WINDOW_AFTER,
            )
            .order_by("booking_opens_at")
            .values_list("pk", flat=True)[:MAX_RUNS]
        )
        return [bookings, overdue_family, payments], runs

    def _queues(self, runs):
        from apps.bookings import holds
        from apps.common.redis import get_redis
        from apps.waitroom import services as waitroom

        get_redis().ping()
        at = waitroom.now()
        waiting = GaugeMetricFamily(
            "railrush_waitroom_waiting", "People waiting in the queue of a run.", labels=["run_id"]
        )
        issued = GaugeMetricFamily(
            "railrush_waitroom_tickets_issued", "Tickets issued so far for a run.", labels=["run_id"]
        )
        active = GaugeMetricFamily("railrush_active_holds", "Seats currently held in a run.", labels=["run_id"])
        for run_id in runs:
            info = waitroom.get_run_info(run_id)
            status = waitroom.public_status(info, at)
            waiting.add_metric([str(run_id)], status["people_waiting"])
            issued.add_metric([str(run_id)], status["tickets_issued"])
            active.add_metric([str(run_id)], len(holds.active_labels(run_id, at)))
        return [waiting, issued, active]


@never_cache
@require_GET
def metrics_view(request):
    if request.META.get("HTTP_X_FORWARDED_FOR"):
        # nginx adds this header to everything it proxies, so a request that carries it came from outside.
        raise Http404
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    state = CollectorRegistry()
    state.register(StateCollector())
    body = generate_latest(registry) + generate_latest(state)
    return HttpResponse(body, content_type=CONTENT_TYPE_LATEST)
