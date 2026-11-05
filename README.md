# RailRush

A Django backend for Eid-rush train ticket booking. It never sells a seat twice, and it is built to survive the
minutes when booking opens (design target: 35,000 people at once) on limited hardware.

Status: backend phases 0 to 8 are built and tested; Phase 9 (documentation and CI) is in progress. Measured
capacity comes from one slow laptop and is documented, with its limits, in `docs/CAPACITY.md`.

## What it does

- Accounts with stateless JWT sign-in
- A catalog of stations, trains, runs and fares, with a public seat map
- A virtual waiting room per run, so admissions arrive at a pace the servers can handle
- Seat holds (up to 4 seats, 5 minutes) that no two people can get at once
- Bookings and payments: idempotent booking from holds, a mock payment gateway with signed webhooks, and automatic
  expiry of unpaid bookings
- Metrics, alerts and a Grafana dashboard

## Stack

Django 5.2 and DRF, PostgreSQL 16 behind PgBouncer (transaction pooling), Redis 7, Celery (worker and beat), nginx,
gunicorn (gthread), Docker Compose. Python 3.13.

## Run it locally

Requirements: Docker Desktop, and PowerShell on Windows (the scripts are PowerShell).

1. Copy `.env.example` to `.env` and replace every placeholder:
   - Generate each password and secret yourself. In PowerShell, one random 48-byte hex value:

         $b = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') })

   - Set the PgBouncer image (the tag used so far is `edoburu/pgbouncer:v1.26.0-p0`).
   - If a host port is taken, change `NGINX_HOST_PORT`. The default is 8080; the original build machine uses 8088.
   - `.env` is never committed.
2. Start the stack:

       docker compose up -d --build

   nginx listens on `127.0.0.1` only. Check `http://127.0.0.1:<NGINX_HOST_PORT>/nginx-health` from the host.
3. Optional demo data: `docker compose exec -T web python manage.py seed_demo` (see `--help` for its options).
4. Optional observability: copy the four image lines from `deploy/observability-images.env` into `.env`, set the
   Grafana and exporter passwords, then run:

       docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d

   Grafana is at `http://127.0.0.1:3030`.

After recreating `web`, run `docker compose restart nginx`.

## Tests and checks

- Full suite against real PostgreSQL and Redis:

      docker compose --profile test run --rm test

  The Redis-backed tests cannot be skipped there. On a plain host (SQLite, no Redis) they skip.
- Security gate (lint, bandit, dependency audit, secrets scan, Django checks, migration drift, tests):

      .\scripts\security-check.ps1 -Phase <name>

- End-to-end smoke scripts per phase are in `scripts\`. Load tests are in `loadtest\` (see its README).

## API in one list

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/register/`, `login/`, `refresh/` | Accounts and tokens |
| `GET /api/users/me/` | Current user |
| `GET /api/catalog/stations/`, `runs/`, `runs/<id>/`, `runs/<id>/seats/` | Public catalog and seat map |
| `GET /api/waitroom/<run>/status/`, `POST .../join/`, `GET .../me/` | Waiting room |
| `GET, POST /api/holds/<run>/`, `POST .../release/` | Seat holds (need a waiting-room pass) |
| `POST /api/bookings/`, `GET /api/bookings/`, `GET /api/bookings/<id>/` | Bookings |
| `POST /api/bookings/<id>/pay/`, `.../cancel/` | Payment and cancellation |
| `POST /api/payments/webhook/` | Gateway callback (HMAC-signed) |
| `GET /api/health/live/`, `ready/` | Health probes |

## Documentation

- `docs/ARCHITECTURE.md`: components, data, the purchase flow, failure behaviour, known gaps
- `docs/adr/`: decision records
- `docs/CAPACITY.md`: measured capacity and the sizing formula
- `docs/RUNBOOK.md`: operating procedures
- `docs/phase-reports/`: one Word report per build phase

## Repository layout

    apps/        Django apps: accounts, catalog, waitroom, bookings, common
    config/      Settings (base, dev, prod), URLs, Celery
    deploy/      nginx, PostgreSQL, Redis, gunicorn, Prometheus and Grafana configuration
    docs/        Architecture, ADRs, capacity, runbook, phase reports
    loadtest/    k6 load-test harness and capacity tools
    scripts/     Security gate and smoke tests
    requirements/ Python dependencies (ranges, no lock file yet)