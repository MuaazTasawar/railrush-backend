from types import SimpleNamespace

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from .models import Coach, Fare, SeatClass, Station, Train


@pytest.fixture(autouse=True)
def _clean_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def network(db):
    """Three stations and one KHI-LHE train: 60 economy seats and 12 AC Business seats."""
    khi = Station.objects.create(code="KHI", name="Karachi Cantonment", city="Karachi")
    lhe = Station.objects.create(code="LHE", name="Lahore Junction", city="Lahore")
    pew = Station.objects.create(code="PEW", name="Peshawar Cantonment", city="Peshawar")
    train = Train.objects.create(number="101", name="Eid Express", origin=khi, destination=lhe)
    Coach.objects.create(train=train, number=1, seat_class=SeatClass.ECONOMY, rows=10, columns="ABCD")
    Coach.objects.create(train=train, number=2, seat_class=SeatClass.ECONOMY, rows=5, columns="ABCD")
    Coach.objects.create(train=train, number=3, seat_class=SeatClass.AC_BUSINESS, rows=4, columns="ABC")
    Fare.objects.create(train=train, seat_class=SeatClass.ECONOMY, amount_pkr=2500)
    Fare.objects.create(train=train, seat_class=SeatClass.AC_BUSINESS, amount_pkr=8500)
    return SimpleNamespace(khi=khi, lhe=lhe, pew=pew, train=train)
