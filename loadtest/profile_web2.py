"""Second profile: what do the throttles cost, and why is a database-backed endpoint slow? Read-only.
Requests go through Django's test client (no gunicorn, nginx or network). Piped into manage.py shell."""
import cProfile
import io
import pstats
import statistics
import time

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.bookings.booking_views import BookingsView
from apps.bookings.views import HoldsView
from apps.catalog.models import TrainRun


def summarize(label, samples, codes):
    ordered = sorted(samples)
    count = len(ordered)
    print("%-44s n=%-4d mean=%8.3f p50=%8.3f p95=%8.3f ms  codes=%s" % (
        label, count, statistics.mean(ordered), ordered[count // 2], ordered[min(count - 1, int(count * 0.95))], codes))


users = list(User.objects.filter(email__startswith="lt-", email__endswith="@loadtest.example.test").order_by("email")[:20])
if not users:
    print("No load-test users found: run .\\loadtest\\seed.ps1 -Users 40 -Runs 1 first.")
else:
    tokens = [str(RefreshToken.for_user(u).access_token) for u in users]
    run = TrainRun.objects.order_by("-id").first()
    run_id = run.id if run else 1
    client = Client(HTTP_HOST="localhost")
    counter = [0]

    def request(path):
        counter[0] += 1
        n = counter[0]
        return client.get(
            path,
            HTTP_AUTHORIZATION="Bearer " + tokens[n % len(tokens)],
            HTTP_X_FORWARDED_FOR="10.9.%d.%d" % (n // 250, n % 250 + 1),
        )

    def timed(label, path, n=200):
        for _ in range(30):
            request(path)
        samples = []
        codes = {}
        for _ in range(n):
            started = time.perf_counter()
            response = request(path)
            samples.append((time.perf_counter() - started) * 1000.0)
            codes[response.status_code] = codes.get(response.status_code, 0) + 1
        summarize(label, samples, codes)

    holds_path = "/api/holds/%d/" % run_id
    print("db settings: CONN_MAX_AGE=%s CONN_HEALTH_CHECKS=%s" % (
        connection.settings_dict.get("CONN_MAX_AGE"), connection.settings_dict.get("CONN_HEALTH_CHECKS")))

    print("\n== what do the DRF throttles cost? same request with throttles on and off")
    try:
        timed("GET /api/holds/<run>/ throttles ON", holds_path)
        HoldsView.throttle_classes = []
        timed("GET /api/holds/<run>/ throttles OFF", holds_path)
    finally:
        if "throttle_classes" in HoldsView.__dict__:
            del HoldsView.throttle_classes
    try:
        timed("GET /api/bookings/ throttles ON", "/api/bookings/")
        BookingsView.throttle_classes = []
        timed("GET /api/bookings/ throttles OFF", "/api/bookings/")
    finally:
        if "throttle_classes" in BookingsView.__dict__:
            del BookingsView.throttle_classes

    print("\n== queries behind one GET /api/bookings/ (time as Django measured it)")
    request("/api/bookings/")
    with CaptureQueriesContext(connection) as ctx:
        request("/api/bookings/")
    print("%d queries" % len(ctx))
    for q in ctx.captured_queries:
        print("   %6.2f ms  %s" % (float(q["time"]) * 1000.0, " ".join(q["sql"].split())[:110]))

    print("\n== cProfile of 150 GET /api/bookings/ (profiling roughly doubles the time)")
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(150):
        request("/api/bookings/")
    profiler.disable()
    for title, key, rows in (("by own time", "tottime", 20), ("by cumulative time", "cumulative", 26)):
        out = io.StringIO()
        pstats.Stats(profiler, stream=out).strip_dirs().sort_stats(key).print_stats(rows)
        print("---- " + title + " ----")
        print(out.getvalue())
