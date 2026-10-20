"""Booking and payment shapes. Prices are whole Pakistani rupees, as in the catalog."""

from rest_framework import serializers

from .models import Booking, BookingSeat, Payment


class BookingCreateSerializer(serializers.Serializer):
    run_id = serializers.IntegerField(min_value=1, max_value=10**18 - 1)


class BookingSeatSerializer(serializers.ModelSerializer):
    seat = serializers.CharField(source="seat_label")

    class Meta:
        model = BookingSeat
        fields = ["seat", "seat_class", "price_pkr"]


class BookingSerializer(serializers.ModelSerializer):
    run_id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    seats = BookingSeatSerializer(many=True, read_only=True)

    class Meta:
        model = Booking
        fields = [
            "id",
            "run_id",
            "status",
            "total_pkr",
            "expires_at",
            "confirmed_at",
            "created_at",
            "seats",
        ]


class PaymentSerializer(serializers.ModelSerializer):
    status = serializers.CharField(read_only=True)

    class Meta:
        model = Payment
        fields = ["id", "provider", "provider_ref", "amount_pkr", "status", "created_at"]
