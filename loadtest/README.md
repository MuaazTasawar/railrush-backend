# Load tests (Phase 8)

Scripts only: tokens and results live outside the repository, in ..\railrush-loadtest (never commit them).
k6 runs inside the compose network (docker run grafana/k6), so nothing needs installing.

    .\loadtest\up.ps1                                       load-test mode: nginx trusts X-Forwarded-For, 60 s payment window
    .\loadtest\rushcheck.ps1 -Users 400 -Runs 2 -Label x    one run: seed, k6 journey, one result row, data invariants, clean up
    .\loadtest\sweep.ps1                                    admission-rate sweep
    .\loadtest\phase8b.ps1                                  join bursts and process models
    .\loadtest\ab.ps1                                       old versus fast throttles (FAST_THROTTLES)
    .\loadtest\capab.ps1                                    in-flight cap on versus off (LT_INFLIGHT, load-test nginx only)
    .\loadtest\down.ps1                                     back to the production configuration (always finish with this)

Switches (environment variables read by up.ps1 and docker-compose.loadtest.yml): LT_ADMIT_PER_SECOND, LT_FAST_THROTTLES,
LT_INFLIGHT, LT_BOOKING_SECONDS, WEB_CONCURRENCY, GUNICORN_WORKER_CLASS, APP_MODULE.

loadtest\bench_cpu.py measures the machine's speed. Every result row records it, because on a throttled laptop it
swings by 5 times. The profile_*.py scripts are piped into `manage.py shell` inside the web container.

Never use load-test mode in production: it trusts a client-supplied address and shortens the payment window.
