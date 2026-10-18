"""Load fictional demo data: stations, trains, coaches, seats, fares and daily runs.

Safe to run repeatedly: existing rows are updated or skipped, never duplicated. Schedules and
fares are made up for development and load testing; they are not real timetable data.
"""

from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import Coach, Fare, Seat, SeatClass, Station, Train, TrainRun
from apps.catalog.timeutils import catalog_tz, local_today

STATIONS = [
    ("KHI", "Karachi Cantonment", "Karachi"),
    ("HYD", "Hyderabad Junction", "Hyderabad"),
    ("SKZ", "Sukkur Junction", "Sukkur"),
    ("MUX", "Multan Cantonment", "Multan"),
    ("LHE", "Lahore Junction", "Lahore"),
    ("RWP", "Rawalpindi", "Rawalpindi"),
    ("PEW", "Peshawar Cantonment", "Peshawar"),
    ("FSD", "Faisalabad", "Faisalabad"),
    ("QTA", "Quetta", "Quetta"),
    ("SGD", "Sargodha Junction", "Sargodha"),
]

# number, name, origin, destination, local departure time, journey minutes
TRAINS = [
    ("101", "Karachi-Lahore Eid Express", "KHI", "LHE", "18:30", 1080),
    ("102", "Lahore-Karachi Eid Express", "LHE", "KHI", "17:00", 1080),
    ("201", "Karachi-Rawalpindi Eid Special", "KHI", "RWP", "15:30", 1320),
    ("202", "Rawalpindi-Karachi Eid Special", "RWP", "KHI", "14:00", 1320),
    ("301", "Lahore-Peshawar Express", "LHE", "PEW", "07:30", 420),
    ("302", "Peshawar-Lahore Express", "PEW", "LHE", "14:30", 420),
]

# seat class, number of coaches, rows, seat letters
COACH_PLAN = [
    (SeatClass.ECONOMY, 4, 18, "ABCD"),
    (SeatClass.AC_STANDARD, 3, 16, "ABCD"),
    (SeatClass.AC_BUSINESS, 1, 10, "ABCD"),
]

FARES_PKR = {
    SeatClass.ECONOMY: 2500,
    SeatClass.AC_STANDARD: 5500,
    SeatClass.AC_BUSINESS: 8500,
}


class Command(BaseCommand):
    help = "Create or refresh fictional demo catalog data (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="Days of runs to create, 1 to 120.")
        parser.add_argument(
            "--start",
            type=date.fromisoformat,
            default=None,
            help="First departure date as YYYY-MM-DD (default: tomorrow, local time).",
        )
        parser.add_argument(
            "--booking-window-days",
            type=int,
            default=7,
            help="Booking opens this many days before departure, 1 to 60.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        days = options["days"]
        window = options["booking_window_days"]
        if not 1 <= days <= 120:
            raise CommandError("--days must be between 1 and 120.")
        if not 1 <= window <= 60:
            raise CommandError("--booking-window-days must be between 1 and 60.")
        start = options["start"] or local_today() + timedelta(days=1)
        tz = catalog_tz()

        stations = {}
        for code, name, city in STATIONS:
            stations[code], _ = Station.objects.update_or_create(
                code=code, defaults={"name": name, "city": city}
            )

        new_runs = 0
        for number, name, origin, destination, departs, minutes in TRAINS:
            train, _ = Train.objects.update_or_create(
                number=number,
                defaults={
                    "name": name,
                    "origin": stations[origin],
                    "destination": stations[destination],
                },
            )
            self._ensure_coaches_and_seats(train)
            for seat_class, amount in FARES_PKR.items():
                Fare.objects.update_or_create(
                    train=train, seat_class=seat_class, defaults={"amount_pkr": amount}
                )
            hour, minute = (int(part) for part in departs.split(":"))
            for offset in range(days):
                departure = datetime.combine(
                    start + timedelta(days=offset), time(hour, minute), tzinfo=tz
                )
                _, created = TrainRun.objects.get_or_create(
                    train=train,
                    departure_at=departure,
                    defaults={
                        "arrival_at": departure + timedelta(minutes=minutes),
                        "booking_opens_at": departure - timedelta(days=window),
                    },
                )
                new_runs += int(created)

        self.stdout.write(
            self.style.SUCCESS(
                f"Demo catalog ready: {Station.objects.count()} stations, "
                f"{Train.objects.count()} trains, {Seat.objects.count()} seats, "
                f"{TrainRun.objects.count()} runs ({new_runs} new)."
            )
        )

    @staticmethod
    def _ensure_coaches_and_seats(train):
        number = 0
        for seat_class, count, rows, columns in COACH_PLAN:
            for _ in range(count):
                number += 1
                coach, _created = Coach.objects.update_or_create(
                    train=train,
                    number=number,
                    defaults={"seat_class": seat_class, "rows": rows, "columns": columns},
                )
                if coach.seats.count() != rows * len(columns):
                    Seat.objects.bulk_create(
                        [Seat(coach=coach, row=r, column=c) for r in range(1, rows + 1) for c in columns],
                        ignore_conflicts=True,
                    )
