from django.urls import re_path

from .views import HoldsView, ReleaseView, SeatMapView

# At most 18 digits, so an absurdly large id is a plain 404 and never reaches Redis or the database.
urlpatterns = [
    re_path(r"^catalog/runs/(?P<run_id>[0-9]{1,18})/seats/$", SeatMapView.as_view(), name="seat-map"),
    re_path(r"^holds/(?P<run_id>[0-9]{1,18})/$", HoldsView.as_view(), name="holds"),
    re_path(r"^holds/(?P<run_id>[0-9]{1,18})/release/$", ReleaseView.as_view(), name="holds-release"),
]
