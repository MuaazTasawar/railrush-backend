from django.urls import path

from . import health

app_name = "common"

urlpatterns = [
    path("live/", health.live, name="live"),
    path("ready/", health.ready, name="ready"),
]
