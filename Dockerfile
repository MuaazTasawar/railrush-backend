# syntax=docker/dockerfile:1

# ---- base: Python plus a non-root user ------------------------------------------------------
FROM python:3.13-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

# ---- builder: install dependencies into a virtualenv ----------------------------------------
FROM base AS builder
RUN python -m venv /opt/venv
COPY requirements/base.txt requirements/base.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements/base.txt

# ---- test-deps: the same virtualenv plus dev tools (used only by the test image) ------------
FROM builder AS test-deps
COPY requirements/dev.txt requirements/dev.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements/dev.txt

# ---- runtime: what actually runs in production ----------------------------------------------
# pip is removed: nothing needs it at runtime, and its vendored copies of urllib3, msgpack and
# setuptools would otherwise show up in vulnerability scans for code the app never executes.
FROM base AS runtime
ENV PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings.prod
COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app manage.py ./
COPY --chown=app:app config ./config
COPY --chown=app:app apps ./apps
COPY --chown=app:app deploy/gunicorn.conf.py ./deploy/gunicorn.conf.py
RUN find /app/apps -name "test_*.py" -delete \
    && rm -rf /opt/venv/lib/python3.13/site-packages/pip /opt/venv/lib/python3.13/site-packages/pip-*.dist-info \
              /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
    && rm -f /opt/venv/bin/pip /opt/venv/bin/pip3 /opt/venv/bin/pip3.13 \
             /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "config.wsgi:application"]

# ---- test: dev tools and the test files (never deployed) -------------------------------------
FROM base AS test
ENV PATH="/opt/venv/bin:${PATH}" \
    DJANGO_SETTINGS_MODULE=config.settings.prod
COPY --from=test-deps /opt/venv /opt/venv
COPY --chown=app:app manage.py ./
COPY --chown=app:app config ./config
COPY --chown=app:app apps ./apps
COPY pytest.ini ./
USER 10001:10001
CMD ["pytest", "-q"]
