"""Read-only shapes for the public catalog."""

from datetime import timedelta

from django.conf import settings
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Coach, SeatClass, Station, TrainRun
from .timeutils import local_today


class StationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Station
        fields = ("code", "name", "city")


class CoachSerializer(serializers.ModelSerializer):
    seat_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Coach
        fields = ("number", "seat_class", "rows", "columns", "seat_count")


class ClassOfferSerializer(serializers.Serializer):
    """Schema hint for the computed per-class summary on a run."""

    seat_class = serializers.ChoiceField(choices=SeatClass.choices)
    fare_pkr = serializers.IntegerField(allow_null=True)
    seat_count = serializers.IntegerField()


class RunSerializer(serializers.ModelSerializer):
    train_number = serializers.CharField(source="train.number", read_only=True)
    train_name = serializers.CharField(source="train.name", read_only=True)
    origin = StationSerializer(source="train.origin", read_only=True)
    destination = StationSerializer(source="train.destination", read_only=True)
    classes = serializers.SerializerMethodField()

    class Meta:
        model = TrainRun
        fields = (
            "id",
            "train_number",
            "train_name",
            "origin",
            "destination",
            "departure_at",
            "arrival_at",
            "booking_opens_at",
            "status",
            "classes",
        )

    @extend_schema_field(ClassOfferSerializer(many=True))
    def get_classes(self, run):
        # Uses the prefetched coaches and fares, so listing runs costs no extra queries per row.
        capacity = {}
        for coach in run.train.coaches.all():
            capacity[coach.seat_class] = capacity.get(coach.seat_class, 0) + coach.seat_count
        fares = {fare.seat_class: fare.amount_pkr for fare in run.train.fares.all()}
        return [
            {"seat_class": value, "fare_pkr": fares.get(value), "seat_count": capacity[value]}
            for value in SeatClass.values
            if value in capacity
        ]


class RunDetailSerializer(RunSerializer):
    coaches = CoachSerializer(source="train.coaches", many=True, read_only=True)

    class Meta(RunSerializer.Meta):
        fields = (*RunSerializer.Meta.fields, "coaches")


class RunSearchSerializer(serializers.Serializer):
    """Validates the search query string. Errors never echo the submitted value."""

    origin = serializers.CharField(min_length=2, max_length=5)
    destination = serializers.CharField(min_length=2, max_length=5)
    date = serializers.DateField()

    @staticmethod
    def _station_code(value):
        code = value.strip().upper()
        if not (2 <= len(code) <= 5 and code.isascii() and code.isalpha()):
            raise serializers.ValidationError("Use a 2 to 5 letter station code.")
        return code

    def validate_origin(self, value):
        return self._station_code(value)

    def validate_destination(self, value):
        return self._station_code(value)

    def validate_date(self, value):
        today = local_today()
        if value < today:
            raise serializers.ValidationError("The date cannot be in the past.")
        if value > today + timedelta(days=settings.CATALOG_MAX_DAYS_AHEAD):
            raise serializers.ValidationError("The date is too far ahead.")
        return value
