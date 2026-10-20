"""Celery autodiscovers this module. The booking tasks live in booking_tasks.py."""

from .booking_tasks import expire_pending_bookings

__all__ = ["expire_pending_bookings"]
