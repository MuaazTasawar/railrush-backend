"""Where does the web tier spend its time? Read-only: GET requests through Django's test client (no gunicorn, nginx
or network in the way) plus micro-benchmarks of the pieces every request touches. Piped into manage.py shell."""
import cProfile
import io
import pstats
import statistics
import time

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import Client
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.models import User
from apps.catalog.models import TrainRun
from apps.common.redis import get_redis


def ms(seconds):
    return seconds * 1000.0


def summarize(label, samples):
    ordered = sorted(samples)
    count = len(ordered)
    print("%-44s n=%-4d mean=%8.3f p50=%8.3f p95=%8.3f ms" % (
        label, count, statistics.mean(ordered), ordered[count // 2], ordered[min(count - 1, int(count * 0.95))]))


def bench(label, fn, n=300, warm=30):
    for _ in range(warm):
        fn()
    samples = []
    for _ in range(n):
        started = time.perf_counter()
        fn()
        samples.append(ms(time.perf_counter() - started))
    summarize(label, samples)


print("settings module=%s DEBUG=%s middleware=%d" % (settings.SETTINGS_MODULE, settings.DEBUG, len(settings.MIDDLEWARE)))

print("\n== interpreter speed")
started = time.perf_counter()
total = 0
for i in range(2000000):
    total += i * i
print("2,000,000 loop iterations: %.3f s" % (time.perf_counter() - started))

print("\n== building blocks, each timed on its own (ms per call)")
redis_client = get_redis()
bench("redis PING", lambda: redis_client.ping())
bench("redis GET (missing key)", lambda: redis_client.get("profile:none"))
bench("redis SET with expiry", lambda: redis_client.set("profile:key", "1", ex=30))
bench("django cache set (throttle style)", lambda: cache.set("profile_throttle", [1.0, 2.0, 3.0], 30))
bench("django cache get", lambda: cache.get("profile_throttle"))
cursor = connection.cursor()
bench("postgres SELECT 1 through pgbouncer", lambda: cursor.execute("SELECT 1"))

users = list(User.objects.filter(email__startswith="lt-", email__endswith="@loadtest.example.test").order_by("email")[:20])
if not users:
    print("\nNo load-test users found: run .\\loadtest\\seed.ps1 -Users 40 -Runs 1 first.")
else:
    tokens = [str(RefreshToken.for_user(u).access_token) for u in users]
    bench("JWT access token verify (SimpleJWT)", lambda: AccessToken(tokens[0]))

    run = TrainRun.objects.order_by("-id").first()
    run_id = run.id if run else 1
    client = Client(HTTP_HOST="localhost")
    counter = [0]

    def request(path, authed):
        counter[0] += 1
        n = counter[0]
        extra = {"HTTP_X_FORWARDED_FOR": "10.8.%d.%d" % (n // 250, n % 250 + 1)}
        if authed:
            extra["HTTP_AUTHORIZATION"] = "Bearer " + tokens[n % len(tokens)]
        return client.get(path, **extra)

    def run_target(label, path, authed, n=200):
        for _ in range(30):
            request(path, authed)
        samples = []
        codes = {}
        for _ in range(n):
            started = time.perf_counter()
            response = request(path, authed)
            samples.append(ms(time.perf_counter() - started))
            codes[response.status_code] = codes.get(response.status_code, 0) + 1
        summarize(label, samples)
        print("    status codes: %s" % codes)

    print("\n== whole requests through Django (middleware, auth, throttles, view; no HTTP server)")
    holds_path = "/api/holds/%d/" % run_id
    run_target("GET /api/health/live/ (no auth)", "/api/health/live/", False)
    run_target("GET /api/holds/<run>/ (auth + redis)", holds_path, True)
    run_target("GET /api/bookings/ (auth + database)", "/api/bookings/", True)

    print("\n== cProfile of 200 authenticated GET /api/holds/ (profiling itself roughly doubles the time)")
    profiler = cProfile.Profile()
    profiler.enable()
    for _ in range(200):
        request(holds_path, True)
    profiler.disable()
    out = io.StringIO()
    stats = pstats.Stats(profiler, stream=out)
    stats.strip_dirs().sort_stats("tottime").print_stats(22)
    print("---- by own time (tottime) ----")
    print(out.getvalue())
    out2 = io.StringIO()
    stats2 = pstats.Stats(profiler, stream=out2)
    stats2.strip_dirs().sort_stats("cumulative").print_stats(28)
    print("---- by cumulative time ----")
    print(out2.getvalue())

redis_client.delete("profile:key")
cache.delete("profile_throttle")
