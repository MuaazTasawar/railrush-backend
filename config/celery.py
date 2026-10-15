"""Celery application. Defaults to prod settings; override with DJANGO_SETTINGS_MODULE."""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("railrush")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
