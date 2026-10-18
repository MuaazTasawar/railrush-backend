from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def catalog_tz():
    return ZoneInfo(settings.CATALOG_TIME_ZONE)


def local_today():
    return timezone.now().astimezone(catalog_tz()).date()


def local_day_bounds(day):
    """Start (inclusive) and end (exclusive) of a local calendar day, as aware datetimes."""
    tz = catalog_tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start, end
