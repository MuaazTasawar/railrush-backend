"""Static catalog: stations, trains, coaches, seats and fares, plus scheduled runs.

A seat is sold for a whole journey (the train's origin to its terminus); intermediate stops and
per-leg inventory are out of scope. Seats belong to the train, not to a run. Whether a seat is
free on a given run is decided later by holds (Redis) and bookings (a unique constraint on
run + seat), never by the catalog.
"""

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q


class SeatClass(models.TextChoices):
    ECONOMY = "economy", "Economy"
    AC_STANDARD = "ac_standard", "AC Standard"
    AC_BUSINESS = "ac_business", "AC Business"


class Station(models.Model):
    code = models.CharField(
        max_length=5,
        unique=True,
        validators=[RegexValidator(r"^[A-Z]{2,5}$", "Use 2 to 5 capital letters.")],
    )
    name = models.CharField(max_length=100)
    city = models.CharField(max_length=100)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Train(models.Model):
    number = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=100)
    origin = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="departing_trains")
    destination = models.ForeignKey(Station, on_delete=models.PROTECT, related_name="arriving_trains")

    class Meta:
        ordering = ["number"]
        indexes = [models.Index(fields=["origin", "destination"], name="train_route_idx")]
        constraints = [
            models.CheckConstraint(
                condition=~Q(origin=F("destination")),
                name="train_origin_differs_from_destination",
            )
        ]

    def __str__(self):
        return f"{self.number} {self.name}"


class Coach(models.Model):
    train = models.ForeignKey(Train, on_delete=models.CASCADE, related_name="coaches")
    number = models.PositiveSmallIntegerField()
    seat_class = models.CharField(max_length=20, choices=SeatClass.choices)
    rows = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(60)]
    )
    columns = models.CharField(
        max_length=6,
        validators=[RegexValidator(r"^[A-Z]{1,6}$", "Use 1 to 6 capital letters, e.g. ABCD.")],
        help_text="Seat letters per row, e.g. ABCD.",
    )

    class Meta:
        ordering = ["number"]
        constraints = [
            models.UniqueConstraint(fields=["train", "number"], name="uniq_coach_number_per_train"),
            models.CheckConstraint(
                condition=Q(rows__gte=1) & Q(rows__lte=60), name="coach_rows_between_1_and_60"
            ),
        ]

    @property
    def seat_count(self):
        return self.rows * len(self.columns)

    def __str__(self):
        return f"{self.train.number} coach {self.number}"


class Seat(models.Model):
    coach = models.ForeignKey(Coach, on_delete=models.CASCADE, related_name="seats")
    row = models.PositiveSmallIntegerField()
    column = models.CharField(max_length=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["coach", "row", "column"], name="uniq_seat_per_coach")
        ]

    def __str__(self):
        return f"{self.row}{self.column}"


class Fare(models.Model):
    """Price in whole Pakistani rupees for one seat in a class, whatever the run."""

    train = models.ForeignKey(Train, on_delete=models.CASCADE, related_name="fares")
    seat_class = models.CharField(max_length=20, choices=SeatClass.choices)
    amount_pkr = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["train", "seat_class"], name="uniq_fare_per_class"),
            models.CheckConstraint(condition=Q(amount_pkr__gt=0), name="fare_is_positive"),
        ]


class TrainRun(models.Model):
    """One departure of a train. Its numeric id is the run_id used by the waiting room."""

    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CANCELLED = "cancelled", "Cancelled"

    train = models.ForeignKey(Train, on_delete=models.PROTECT, related_name="runs")
    departure_at = models.DateTimeField()
    arrival_at = models.DateTimeField()
    booking_opens_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SCHEDULED)

    class Meta:
        ordering = ["departure_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["train", "departure_at"], name="uniq_run_per_departure"),
            models.CheckConstraint(
                condition=Q(arrival_at__gt=F("departure_at")), name="run_arrives_after_departure"
            ),
            models.CheckConstraint(
                condition=Q(booking_opens_at__lte=F("departure_at")),
                name="run_booking_opens_before_departure",
            ),
        ]

    def __str__(self):
        return f"{self.train.number} {self.departure_at:%Y-%m-%d %H:%M}"
