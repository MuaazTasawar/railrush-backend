"""Request shapes, plus response shapes that exist only to describe the API schema."""

from django.conf import settings
from rest_framework import serializers


class HoldRequestSerializer(serializers.Serializer):
    seats = serializers.ListField(child=serializers.CharField(max_length=8), allow_empty=False)

    def validate_seats(self, value):
        if len(value) > settings.HOLD_MAX_SEATS_PER_REQUEST:
            raise serializers.ValidationError("Too many seats in one request.")
        return value


class ReleaseRequestSerializer(serializers.Serializer):
    seats = serializers.ListField(
        child=serializers.CharField(max_length=8), allow_empty=False, required=False
    )

    def validate_seats(self, value):
        if len(value) > settings.HOLD_MAX_SEATS_PER_USER:
            raise serializers.ValidationError("Too many seats in one request.")
        return value


class HeldSeatSerializer(serializers.Serializer):
    seat = serializers.CharField()
    expires_at = serializers.DateTimeField()


class HoldsSerializer(serializers.Serializer):
    run_id = serializers.IntegerField()
    hold_seconds = serializers.IntegerField()
    server_time = serializers.DateTimeField()
    seats = HeldSeatSerializer(many=True)


class ReleasedSerializer(serializers.Serializer):
    run_id = serializers.IntegerField()
    released = serializers.ListField(child=serializers.CharField())
    server_time = serializers.DateTimeField()


class CoachMapSerializer(serializers.Serializer):
    number = serializers.IntegerField()
    seat_class = serializers.CharField()
    rows = serializers.IntegerField()
    columns = serializers.CharField()
    unavailable = serializers.ListField(child=serializers.CharField())


class AvailabilitySerializer(serializers.Serializer):
    seat_class = serializers.CharField()
    total = serializers.IntegerField()
    available = serializers.IntegerField()


class SeatMapSerializer(serializers.Serializer):
    run_id = serializers.IntegerField()
    hold_seconds = serializers.IntegerField()
    server_time = serializers.DateTimeField()
    coaches = CoachMapSerializer(many=True)
    availability = AvailabilitySerializer(many=True)
