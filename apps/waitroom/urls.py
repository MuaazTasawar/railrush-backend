from django.urls import re_path

from .views import JoinView, MeView, StatusView

# At most 18 digits, so an absurdly large id is a plain 404 and never reaches Redis or the database.
urlpatterns = [
    re_path(r"^(?P<run_id>[0-9]{1,18})/status/$", StatusView.as_view(), name="status"),
    re_path(r"^(?P<run_id>[0-9]{1,18})/join/$", JoinView.as_view(), name="join"),
    re_path(r"^(?P<run_id>[0-9]{1,18})/me/$", MeView.as_view(), name="me"),
]
