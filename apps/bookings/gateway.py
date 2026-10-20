"""Payment gateway adapter.

Only a mock gateway exists. A real provider (JazzCash, Easypaisa, a card processor) implements
the same small surface: make a reference for a new payment, describe the checkout to the client,
and sign webhooks the way verification here expects (HMAC-SHA256 of the raw request body, hex).
"""

import hashlib
import hmac
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

SIGNATURE_META_KEY = "HTTP_X_GATEWAY_SIGNATURE"


def sign(secret, raw_body):
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def signature_is_valid(raw_body, signature):
    """Constant-time check of the webhook signature. A missing secret or signature never passes."""
    secret = settings.PAYMENT_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = sign(secret, raw_body).encode("ascii")
    received = str(signature).strip().lower().encode("utf-8")
    return hmac.compare_digest(expected, received)


class MockGateway:
    name = "mock"

    def new_reference(self):
        return f"mock_{uuid.uuid4().hex}"

    def checkout(self, payment):
        return {
            "provider": self.name,
            "reference": payment.provider_ref,
            "amount_pkr": payment.amount_pkr,
        }


def get_gateway():
    if settings.PAYMENT_PROVIDER == "mock":
        return MockGateway()
    raise ImproperlyConfigured("Unknown PAYMENT_PROVIDER.")
