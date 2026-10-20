from celery import shared_task

from . import booking_services, holds


@shared_task(name="apps.bookings.tasks.expire_pending_bookings")
def expire_pending_bookings():
    """Runs every 30 seconds from Celery beat. Safe to run twice at once: rows are locked and skipped."""
    return booking_services.expire_pending(holds.now())
