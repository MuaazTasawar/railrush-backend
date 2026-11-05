# ADR 0007: Observability with Prometheus, Grafana and two exporters

Status: Accepted (Phase 8 part 2, October 2026)

Note on sources: the Phase 8 part 2 report records the problems met, not alternatives, apart from the decision to
defer Alertmanager. This ADR is written from `docker-compose.observability.yml`, `deploy/prometheus/`,
`config/settings/prod.py`, `docs/RUNBOOK.md` and that report.

## Context

During a rush the operator needs request rates, errors and latency for the whole web tier (3 gunicorn workers),
business signals (bookings that are stuck, refunds, queue sizes) and the health of Redis and PostgreSQL. The budget
is a laptop-sized machine, and nothing internal may be reachable from outside.

## Decision

`docker-compose.observability.yml` runs next to the main stack:

- **Prometheus** scrapes every 15 seconds and keeps 7 days or 1 GB. It has no published port.
- **Grafana** is published on `127.0.0.1:3030` only. Sign-up, anonymous access and analytics are off. The
  "RailRush overview" dashboard (19 panels) is generated from code and provisioned from `deploy/grafana`.
- **Redis exporter** and **PostgreSQL exporter**. The PostgreSQL exporter uses the `railrush_monitor` role
  (read-only `pg_monitor`, 3 connections, 5 s statement timeout).
- **Web metrics** come from django-prometheus in multiprocess mode (`PROMETHEUS_MULTIPROC_DIR` on a tmpfs, web
  container only; `child_exit` marks dead workers). Without it, each scrape would show one random worker's
  counters. After the change a probe read the same counter value (63) on 8 consecutive scrapes.
- **Business metrics**: `railrush_*` gauges read from PostgreSQL and Redis when Prometheus scrapes, and a counter
  of payment-event results (`apps/common/metrics.py`).
- **Exposure**: `/internal/metrics` refuses any request carrying `X-Forwarded-For` (anything that came through
  nginx), nginx answers 404 for `/internal/`, Prometheus scrapes `web:8000` directly on the private network, and
  production settings exempt `/internal/` from the HTTPS redirect.
- **Alerts**: 13 rules in `deploy/prometheus/alerts.yml`, unit-tested with `promtool test rules`.
- **Image digests** for the four images are pinned in `.env`, with a copy in `deploy/observability-images.env`,
  because an unpinned rebuild had already changed versions once during Phase 8.

## Consequences

- There is no Alertmanager. Firing alerts show in Prometheus and on the dashboard, and nobody is notified. A real
  deployment needs one.
- There are no nginx metrics and no per-container metrics. CPU per container is read with `docker stats`
  (see the runbook).
- The four containers have memory limits totalling 928 MiB; about 400 MiB was measured in use in Phase 8 part 2.
- The `railrush_*` gauges are computed at scrape time, so every scrape reads PostgreSQL and Redis. The
  `MetricSourceFailing` alert covers a source that cannot be read.
- Dashboards and alerts depend on the metric names, so renaming a metric means updating both.