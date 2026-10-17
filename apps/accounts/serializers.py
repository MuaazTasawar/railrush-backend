"""Request and response shapes for the accounts API.

Only the fields declared here are ever read from a request body, so a client cannot set
is_staff, is_superuser, is_active or id by adding them to the JSON (mass assignment).
"""

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework import serializers

from apps.common.exceptions import Conflict

from .models import User

EMAIL_TAKEN = "An account with this email already exists."


class UserSerializer(serializers.ModelSerializer):
    """What a client may see about an account. Only full_name can be changed."""

    class Meta:
        model = User
        fields = ("id", "email", "full_name", "date_joined")
        read_only_fields = ("id", "email", "date_joined")

    def update(self, instance, validated_data):
        instance.full_name = validated_data.get("full_name", instance.full_name)
        instance.save(update_fields=["full_name"])
        return instance


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    # max_length caps the work a hostile client can make the hasher do.
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)
    full_name = serializers.CharField(max_length=150, allow_blank=True, default="")

    def validate_email(self, value):
        email = value.strip().lower()
        # Checked before any password hashing so duplicate sign-ups stay cheap.
        if User.objects.filter(email=email).exists():
            raise Conflict(EMAIL_TAKEN)
        return email

    def validate(self, attrs):
        candidate = User(email=attrs["email"], full_name=attrs.get("full_name", ""))
        try:
            password_validation.validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from None
        return attrs

    def create(self, validated_data):
        try:
            with transaction.atomic():
                return User.objects.create_user(**validated_data)
        except IntegrityError:
            # Two sign-ups for the same email raced past the check above.
            raise Conflict(EMAIL_TAKEN) from None


class LoginSerializer(serializers.Serializer):
    email = serializers.CharField(max_length=254)
    password = serializers.CharField(write_only=True, max_length=128, trim_whitespace=False)

    def validate_email(self, value):
        return value.strip().lower()


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField(max_length=4096)


class AccessTokenSerializer(serializers.Serializer):
    """Response shape of the refresh endpoint (used for the API schema only)."""

    access = serializers.CharField()


class AuthResponseSerializer(serializers.Serializer):
    """Response shape of register and login (used for the API schema only)."""

    user = UserSerializer()
    access = serializers.CharField()
    refresh = serializers.CharField()
