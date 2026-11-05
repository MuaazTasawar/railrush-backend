# ADR 0004: PostgreSQL behind PgBouncer in transaction-pooling mode

Status: Accepted (Phase 2, October 2026)

Note on sources: the Phase 2 report does not record alternatives for this choice, so none are listed. This ADR is
written from `docker-compose.yml`, `deploy/postgres/postgresql.conf`, `config/settings/base.py` and the Phase 8
measurements. The connection counts below are arithmetic from the configuration, not measurements.

## Context

Django opens one database connection per thread and keeps it for `DB_CONN_MAX_AGE` (60 seconds). A web container
runs 3 workers of 16 threads (ADR 0003), so it can hold up to 48 connections, and the Celery worker adds 2
processes. PostgreSQL is set to `max_connections = 100`, and a rush adds web containers, not database capacity.

## Decision

The application connects to PgBouncer (port 6432) in transaction-pooling mode, and PgBouncer opens a small number of
real PostgreSQL connections. Pool settings (`docker-compose.yml`):

| Setting | Value |
|---|---|
| `POOL_MODE` | transaction |
| `MAX_CLIENT_CONN` | 1000 |
| `DEFAULT_POOL_SIZE` / `RESERVE_POOL_SIZE` | 20 / 5 |
| `MAX_DB_CONNECTIONS` | 40 |
| `QUERY_WAIT_TIMEOUT` | 10 s |
| `IDLE_TRANSACTION_TIMEOUT` | 15 s |
| `AUTH_TYPE` | scram-sha-256 |

PostgreSQL also cuts idle transactions at 15 seconds (`idle_in_transaction_session_timeout`) and gives up on lock
waits after 5 seconds (`lock_timeout`).

Django is configured for it (`config/settings/base.py`): server-side cursors are disabled, server-side prepared
statements are disabled (`prepare_threshold = None`), `CONN_HEALTH_CHECKS` is on, and `ATOMIC_REQUESTS` is off.
PgBouncer ignores the `extra_float_digits` and `options` startup parameters that psycopg sends. The PgBouncer image
is pinned by tag (`edoburu/pgbouncer:v1.26.0-p0`), not by digest.

Two services bypass PgBouncer: the one-shot `migrate` service and the PostgreSQL exporter connect straight to
`postgres:5432`. The compose file records the choice for migrations, not the reason.

## Consequences

- Session state does not survive between transactions. Code for this stack must stay with transaction-scoped
  features (for example `select_for_update` inside a transaction).
- `MAX_DB_CONNECTIONS = 40` leaves room in PostgreSQL's 100 for the superuser reserve (3), the exporter (3) and
  migrations. By the same arithmetic, `MAX_CLIENT_CONN = 1000` covers about 20 web containers of 48 threads, plus
  the Celery worker.
- When all server connections are busy, clients wait up to 10 seconds and then get an error. The API does not yet
  turn that into a 503; this is on the open list.
- Measured cost (`docs/CAPACITY.md`): PgBouncer used 0.012 to 0.044 core-seconds per user depending on the
  machine's speed, which is about 6 to 8% of the whole stack's CPU on the test rig.