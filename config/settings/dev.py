# ruff: noqa: F403, F405
"""Local development settings."""

from .base import *

DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])
API_DOCS_ENABLED = env.bool("API_DOCS_ENABLED", default=True)

# Until Redis runs locally (Phase 2), keep the cache in-process.
# Set DEV_LOCMEM_CACHE=False in .env once the Docker stack is up.
if env.bool("DEV_LOCMEM_CACHE", default=True):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
