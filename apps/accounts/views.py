"""Accounts API: register, login, token refresh and the signed-in user's profile.

Security notes
- Access tokens are stateless (no database hit per request). The profile and refresh endpoints
  do check the database, so a deactivated account loses profile access immediately and can no
  longer mint tokens; an access token it already holds stops working when it expires
  (15 minutes by default).
- A failed login says the same thing whether the email exists or not.
- Token responses are never cacheable.
"""

import hashlib
import logging

from django.contrib.auth import authenticate
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.common.throttling import (
    LoginAccountThrottle,
    LoginIPThrottle,
    RefreshThrottle,
    RegisterThrottle,
)

from .exceptions import InvalidCredentials, InvalidRefreshToken
from .models import User
from .serializers import (
    AccessTokenSerializer,
    AuthResponseSerializer,
    LoginSerializer,
    RefreshSerializer,
    RegisterSerializer,
    UserSerializer,
)

logger = logging.getLogger(__name__)


def issue_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def _account_tag(email):
    """Short one-way tag for logs, so failed logins can be correlated without storing emails."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


def _active_user_exists(user_id):
    if not user_id:
        return False
    try:
        return User.objects.filter(pk=user_id, is_active=True).exists()
    except (ValueError, DjangoValidationError):
        return False


@method_decorator(never_cache, name="dispatch")
class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegisterThrottle]

    @extend_schema(request=RegisterSerializer, responses={201: AuthResponseSerializer})
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        body = {"user": UserSerializer(user).data, **issue_tokens(user)}
        return Response(body, status=status.HTTP_201_CREATED)


@method_decorator(never_cache, name="dispatch")
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginIPThrottle, LoginAccountThrottle]

    @extend_schema(request=LoginSerializer, responses={200: AuthResponseSerializer})
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        # ModelBackend does equal work for unknown emails and rejects inactive accounts.
        user = authenticate(
            request=request, email=email, password=serializer.validated_data["password"]
        )
        if user is None:
            logger.warning("login_failed account=%s", _account_tag(email))
            raise InvalidCredentials()
        return Response({"user": UserSerializer(user).data, **issue_tokens(user)})


@method_decorator(never_cache, name="dispatch")
class RefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RefreshThrottle]

    @extend_schema(request=RefreshSerializer, responses={200: AccessTokenSerializer})
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            refresh = RefreshToken(serializer.validated_data["refresh"])
        except TokenError:
            raise InvalidRefreshToken() from None
        if not _active_user_exists(refresh.get("user_id")):
            raise InvalidRefreshToken()
        return Response({"access": str(refresh.access_token)})


@method_decorator(never_cache, name="dispatch")
class ProfileView(RetrieveUpdateAPIView):
    """The signed-in user's own profile. PUT is not offered; PATCH changes full_name only."""

    serializer_class = UserSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        # request.user is built from token claims; this is the database check.
        user = User.objects.filter(pk=self.request.user.id, is_active=True).first()
        if user is None:
            raise NotAuthenticated("This account is no longer available.")
        return user
