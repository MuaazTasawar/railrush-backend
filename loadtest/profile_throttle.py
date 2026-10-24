"""A/B: DRF's default throttles versus the fast fixed-window throttles versus none, interleaved over several rounds so
drift on a noisy machine shows up. Read-only (GET requests). Piped into manage.py shell."""
import statistics
import time

from django.test import Client
from rest_framework import throttling as drf_throttling
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.bookings.booking_views import BookingsView
from apps.bookings.views import HoldsView
from apps.catalog.models import TrainRun
from apps.common import fast_throttle

OLD = [drf_throttling.AnonRateThrottle, drf_throttling.UserRateThrottle, drf_throttling.ScopedRateThrottle]
NEW = [fast_throttle.FastAnonRateThrottle, fast_throttle.FastUserRateThrottle, fast_throttle.FastScopedRateThrottle]
VARIANTS = (("OLD (DRF cache)", OLD), ("NEW (fast)", NEW), ("OFF", []))
ROUNDS = 3
N = 150

users = list(User.objects.filter(email__startswith="lt-", email__endswith="@loadtest.example.test").order_by("email")[:20])
if not users:
    print("No load-test users found: run .\\loadtest\\seed.ps1 -Users 40 -Runs 1 first.")
else:
    tokens = [str(RefreshToken.for_user(u).access_token) for u in users]
    run = TrainRun.objects.order_by("-id").first()
    run_id = run.id if run else 1
    client = Client(HTTP_HOST="localhost")
    counter = [0]
    bad = [0]

    def request(path):
        counter[0] += 1
        n = counter[0]
        return client.get(
            path,
            HTTP_AUTHORIZATION="Bearer " + tokens[n % len(tokens)],
            HTTP_X_FORWARDED_FOR="10.9.%d.%d" % (n // 250, n % 250 + 1),
        )

    def measure(view, path, classes):
        view.throttle_classes = classes
        for _ in range(20):
            request(path)
        samples = []
        for _ in range(N):
            started = time.perf_counter()
            response = request(path)
            samples.append((time.perf_counter() - started) * 1000.0)
            if response.status_code != 200:
                bad[0] += 1
        return samples

    targets = (
        ("GET /api/holds/<run>/", HoldsView, "/api/holds/%d/" % run_id),
        ("GET /api/bookings/", BookingsView, "/api/bookings/"),
    )
    print("fast path active in this container: %s" % fast_throttle._cache_is_redis())
    results = {}
    try:
        for round_no in range(ROUNDS):
            for label, classes in VARIANTS:
                for name, view, path in targets:
                    median = statistics.median(measure(view, path, classes))
                    results.setdefault((name, label), []).append(median)
    finally:
        for _, view, _ in targets:
            if "throttle_classes" in view.__dict__:
                del view.throttle_classes
    print("non-200 responses: %d" % bad[0])
    print("\nmedian ms per request, one value per round (then the average of the rounds):")
    for name, _, _ in targets:
        for label, _ in VARIANTS:
            values = results[(name, label)]
            print("  %-24s %-16s %s  -> %.2f ms" % (
                name, label, " ".join("%7.2f" % v for v in values), statistics.mean(values)))
