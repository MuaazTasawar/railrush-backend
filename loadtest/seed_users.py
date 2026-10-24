"""Creates load-test users and runs and prints tokens. Piped into `manage.py shell` by seed.ps1."""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.catalog.models import Train, TrainRun

USERS = __USERS__
RUNS = __RUNS__
HOURS = __HOURS__
PREFIX = "lt-"
DOMAIN = "@loadtest.example.test"

train = Train.objects.get(number="101")
now = timezone.now()
for i in range(RUNS):
    departure = now + timedelta(days=3, seconds=i)
    run = TrainRun.objects.create(
        train=train,
        departure_at=departure,
        arrival_at=departure + timedelta(hours=18),
        booking_opens_at=now - timedelta(hours=1),
    )
    print("RUN=%d" % run.id)

users = list(
    User.objects.filter(email__startswith=PREFIX, email__endswith=DOMAIN).order_by("email")[:USERS]
)
for start in range(len(users), USERS, 50):
    with transaction.atomic():
        for i in range(start, min(start + 50, USERS)):
            users.append(User.objects.create_user(email="%s%06d%s" % (PREFIX, i, DOMAIN)))
print("USERS=%d" % len(users))
for user in users:
    token = RefreshToken.for_user(user).access_token
    token.set_exp(lifetime=timedelta(hours=HOURS))
    print("TOKEN=%s" % token)
