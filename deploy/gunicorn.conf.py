"""Gunicorn settings for the web container.

Everything worth tuning is an environment variable, so Phase 8 can compare process models
(threads versus ASGI workers) without editing code.
"""

import os

bind = "0.0.0.0:8000"

# Process model. Default: WSGI with threads. Alternative for Phase 8 measurements:
#   GUNICORN_WORKER_CLASS=uvicorn_worker.UvicornWorker  APP_MODULE=config.asgi:application
workers = int(os.environ.get("WEB_CONCURRENCY", "3"))
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.environ.get("GUNICORN_THREADS", "16"))
backlog = 2048

# Load the app once, then fork: workers share most memory (copy-on-write).
preload_app = True

# Fail slow requests instead of letting them pile up behind a stuck worker.
timeout = 30
graceful_timeout = 30
keepalive = 5

# Restart workers periodically to cap the damage from any slow memory leak.
max_requests = 2000
max_requests_jitter = 200

# Heartbeat files in RAM: avoids disk stalls (and works with a read-only root filesystem).
worker_tmp_dir = "/dev/shm"  # nosec B108 - RAM-backed heartbeat dir, the documented gunicorn-in-Docker setting

# Reject oversized request lines and headers before they reach Django.
limit_request_line = 4094
limit_request_fields = 50
limit_request_field_size = 8190

# Logs go to stdout/stderr for Docker. The access log records the path only, never the query
# string, because query strings can carry tokens.
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s "%(m)s %(U)s" %(s)s %(b)s %(L)s'
