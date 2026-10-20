"""Seat layouts and the public seat map.

A seat is named "<coach>-<row><letter>", for example "3-12C". Names are checked against the
train's coach layout, so no seat table is read when someone holds a seat.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from rest_framework import serializers
from rest_framework.exceptions import NotFound

from apps.catalog.models import Coach, SeatClass, TrainRun
from apps.waitroom.services import iso

from . import holds
from .models import BookingSeat

LABEL_RE = re.compile(r"^(\d{1,3})-(\d{1,3})([A-Z])$")
LAYOUT_TTL_SECONDS = 60
MISSING_RUN_TTL_SECONDS = 15


@dataclass(frozen=True)
class CoachLayout:
    number: int
    seat_class: str
    rows: int
    columns: str


@dataclass(frozen=True)
class RunLayout:
    run_id: int
    opens_at: float
    departs_at: float
    cancelled: bool
    coaches: tuple


def get_layout(run_id):
    """The run's schedule and coach layout, cached briefly so a rush costs the database little."""
    key = f"bk:layout:{run_id}"
    cached = cache.get(key)
    if cached == "missing":
        raise NotFound("Run not found.")
    if cached is not None:
        return cached
    row = (
        TrainRun.objects.filter(pk=run_id)
        .values_list("train_id", "booking_opens_at", "departure_at", "status")
        .first()
    )
    if row is None:
        cache.set(key, "missing", MISSING_RUN_TTL_SECONDS)
        raise NotFound("Run not found.")
    train_id, opens_at, departs_at, status = row
    coaches = tuple(
        CoachLayout(*coach)
        for coach in Coach.objects.filter(train_id=train_id)
        .order_by("number")
        .values_list("number", "seat_class", "rows", "columns")
    )
    layout = RunLayout(
        run_id=run_id,
        opens_at=opens_at.timestamp(),
        departs_at=departs_at.timestamp(),
        cancelled=status == TrainRun.Status.CANCELLED,
        coaches=coaches,
    )
    cache.set(key, layout, LAYOUT_TTL_SECONDS)
    return layout


def canonical_label(raw):
    """Tidy a seat name ("1-01a" becomes "1-1A"). Names that do not parse are returned as typed."""
    label = str(raw).strip().upper()
    match = LABEL_RE.match(label)
    if not match:
        return label
    return f"{int(match.group(1))}-{int(match.group(2))}{match.group(3)}"


def validate_labels(layout, labels):
    """Check seat names against the layout. Errors never repeat what the client sent."""
    by_number = {coach.number: coach for coach in layout.coaches}
    clean = []
    for raw in labels:
        match = LABEL_RE.match(str(raw).strip().upper())
        coach = by_number.get(int(match.group(1))) if match else None
        if (
            coach is None
            or not 1 <= int(match.group(2)) <= coach.rows
            or match.group(3) not in coach.columns
        ):
            raise serializers.ValidationError(
                {"seats": ["One or more seats do not exist on this run."]}
            )
        clean.append(f"{coach.number}-{int(match.group(2))}{match.group(3)}")
    if len(set(clean)) != len(clean):
        raise serializers.ValidationError({"seats": ["Each seat can be listed only once."]})
    return clean


def booked_labels(run_id):
    """Seats sold or reserved on this run: every active seat of a pending or confirmed booking."""
    return frozenset(
        BookingSeat.objects.filter(run_id=run_id, active=True).values_list("seat_label", flat=True)
    )


def _seat_order(label):
    match = LABEL_RE.match(label)
    return (int(match.group(2)), match.group(3)) if match else (0, label)


def build_seatmap(layout, at):
    taken = set(holds.active_labels(layout.run_id, at)) | set(booked_labels(layout.run_id))
    by_coach = defaultdict(list)
    for label in taken:
        match = LABEL_RE.match(label)
        if match:
            by_coach[int(match.group(1))].append(label)
    coaches = []
    totals = {}
    for coach in layout.coaches:
        unavailable = sorted(by_coach.get(coach.number, []), key=_seat_order)
        coaches.append(
            {
                "number": coach.number,
                "seat_class": coach.seat_class,
                "rows": coach.rows,
                "columns": coach.columns,
                "unavailable": unavailable,
            }
        )
        total = totals.setdefault(coach.seat_class, [0, 0])
        total[0] += coach.rows * len(coach.columns)
        total[1] += len(unavailable)
    availability = [
        {"seat_class": value, "total": totals[value][0], "available": totals[value][0] - totals[value][1]}
        for value in SeatClass.values
        if value in totals
    ]
    return {
        "run_id": layout.run_id,
        "hold_seconds": settings.HOLD_SECONDS,
        "server_time": iso(at),
        "coaches": coaches,
        "availability": availability,
    }
