from django.urls import path, re_path

from .views import RunDetailView, RunSearchView, StationListView

urlpatterns = [
    path("stations/", StationListView.as_view(), name="stations"),
    path("runs/", RunSearchView.as_view(), name="run-search"),
    # At most 18 digits, so an absurdly large id is a plain 404 and never reaches the database.
    re_path(r"^runs/(?P<pk>[0-9]{1,18})/$", RunDetailView.as_view(), name="run-detail"),
]
