from rest_framework import status
from rest_framework.exceptions import APIException


class InvalidCredentials(APIException):
    """Wrong email or password. Deliberately identical for unknown and wrong-password cases."""

    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Invalid email or password."
    default_code = "invalid_credentials"


class InvalidRefreshToken(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Refresh token is invalid or expired."
    default_code = "invalid_refresh_token"
