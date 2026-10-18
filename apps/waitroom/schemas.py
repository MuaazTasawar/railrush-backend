"""Serializers used only to describe the responses in the OpenAPI schema."""

from rest_framework import serializers


class PublicStatusSerializer(serializers.Serializer):
    run_id = serializers.IntegerField()
    state = serializers.ChoiceField(choices=["opening_soon", "open", "closed"])
    opens_at = serializers.DateTimeField()
    joins_open_at = serializers.DateTimeField()
    admit_per_second = serializers.FloatField()
    tickets_issued = serializers.IntegerField()
    people_waiting = serializers.IntegerField()
    estimated_wait_seconds = serializers.FloatField()
    server_time = serializers.DateTimeField()


class TicketStatusSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=["waiting", "admitted", "expired"])
    ticket = serializers.IntegerField()
    admit_at = serializers.DateTimeField()
    server_time = serializers.DateTimeField()
    wait_seconds = serializers.FloatField(required=False)
    people_ahead = serializers.IntegerField(required=False)
    waitroom_pass = serializers.CharField(required=False)
    pass_expires_at = serializers.DateTimeField(required=False)
