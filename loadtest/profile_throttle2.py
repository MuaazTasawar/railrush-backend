"""Where does the throttle cost come from? Isolated timings, then a whole-request comparison of throttle variants that
are each missing one ingredient, then a profile of the fast variant. Read-only. Piped into manage.py shell."""
import cProfile
import io
import pstats
import statistics
import time
from types import SimpleNamespace

from django.test import Client
from rest_framework import throttling as drf_throttling
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.bookings.views import HoldsView
from apps.catalog.models import TrainRun
from apps.common import fast_throttle
from apps.common.redis import get_redis


def fake_request(i):
    return SimpleNamespace(
        user=SimpleNamespace(pk="user-%d" % (i % 50), is_authenticated=True),
        META={"REMOTE_ADDR": "10.0.0.1"},
        headers={},
    )


def bench(label, fn, n=500, warm=50):
    for i in range(warm):
        fn(i)
    samples = []
    for i in range(n):
        started = time.perf_counter()
        fn(i)
        samples.append((time.perf_counter() - started) * 1000.0)
    ordered = sorted(samples)
    print("%-52s p50=%7.3f mean=%7.3f p95=%7.3f ms" % (
        label, ordered[n // 2], statistics.mean(ordered), ordered[int(n * 0.95)]))


class Noop(drf_throttling.BaseThrottle):
    def allow_request(self, request, view):
        return True


class KeyOnly(fast_throttle.FastUserRateThrottle):
    def allow_request(self, request, view):
        self.key = self.get_cache_key(request, view)
        return True


OLD3 = [drf_throttling.AnonRateThrottle, drf_throttling.UserRateThrottle, drf_throttling.ScopedRateThrottle]
NEW3 = [fast_throttle.FastAnonRateThrottle, fast_throttle.FastUserRateThrottle, fast_throttle.FastScopedRateThrottle]

print("fast path active: %s" % fast_throttle._cache_is_redis())
print("\n== isolated pieces (no HTTP, no view)")
redis_client = get_redis()
bench("redis PING", lambda i: redis_client.ping())
bench("fast_throttle.hit() (Lua call on the shared client)", lambda i: fast_throttle.hit("bench-%d" % (i % 50), 60))
bench("instantiate FastUserRateThrottle", lambda i: fast_throttle.FastUserRateThrottle())
bench("instantiate DRF UserRateThrottle", lambda i: drf_throttling.UserRateThrottle())
bench("FastUserRateThrottle().get_cache_key()", lambda i: fast_throttle.FastUserRateThrottle().get_cache_key(fake_request(i), None))
bench("FastUserRateThrottle().allow_request() (full)", lambda i: fast_throttle.FastUserRateThrottle().allow_request(fake_request(i), None))
bench("DRF UserRateThrottle().allow_request() (cache get+set)", lambda i: drf_throttling.UserRateThrottle().allow_request(fake_request(i), None))
bench("HoldsView().get_throttles() with the default 3 classes", lambda i: HoldsView().get_throttles())

users = list(User.objects.filter(email__startswith="lt-", email__endswith="@loadtest.example.test").order_by("email")[:20])
if not users:
    print("\nNo load-test users found: run .\\loadtest\\seed.ps1 -Users 40 -Runs 1 first.")
else:
    tokens = [str(RefreshToken.for_user(u).access_token) for u in users]
    run = TrainRun.objects.order_by("-id").first()
    path = "/api/holds/%d/" % (run.id if run else 1)
    client = Client(HTTP_HOST="localhost")
    counter = [0]
    bad = [0]

    def request():
        counter[0] += 1
        n = counter[0]
        response = client.get(
            path,
            HTTP_AUTHORIZATION="Bearer " + tokens[n % len(tokens)],
            HTTP_X_FORWARDED_FOR="10.9.%d.%d" % (n // 250, n % 250 + 1),
        )
        if response.status_code != 200:
            bad[0] += 1

    def measure(classes, n=150):
        HoldsView.throttle_classes = classes
        for _ in range(20):
            request()
        samples = []
        for _ in range(n):
            started = time.perf_counter()
            request()
            samples.append((time.perf_counter() - started) * 1000.0)
        return statistics.median(samples)

    variants = (
        ("OFF (no throttle classes)", []),
        ("NOOP (1 class that does nothing)", [Noop]),
        ("KEYONLY (builds the key, no redis)", [KeyOnly]),
        ("NEW x1 (user throttle only)", [fast_throttle.FastUserRateThrottle]),
        ("NEW x3 (anon, user, scoped)", NEW3),
        ("OLD x3 (DRF cache throttles)", OLD3),
    )
    results = {}
    try:
        for round_no in range(3):
            for label, classes in variants:
                results.setdefault(label, []).append(measure(classes))
        print("\n== whole GET /api/holds/<run>/ through Django, median ms per round and average (non-200: %d)" % bad[0])
        for label, _ in variants:
            values = results[label]
            print("  %-38s %s -> %.2f ms" % (label, " ".join("%7.2f" % v for v in values), statistics.mean(values)))

        print("\n== cProfile of 150 requests with NEW x3 (profiling roughly doubles the times)")
        HoldsView.throttle_classes = NEW3
        profiler = cProfile.Profile()
        profiler.enable()
        for _ in range(150):
            request()
        profiler.disable()
        for title, key in (("by own time", "tottime"), ("by cumulative time", "cumulative")):
            out = io.StringIO()
            pstats.Stats(profiler, stream=out).strip_dirs().sort_stats(key).print_stats("throttl|fast_throttle|redis|script|connection|client", 22)
            print("---- " + title + ", rows that mention throttles, redis or connections ----")
            print(out.getvalue())
    finally:
        if "throttle_classes" in HoldsView.__dict__:
            del HoldsView.throttle_classes
