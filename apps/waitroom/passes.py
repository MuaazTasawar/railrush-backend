"""Waiting-room passes: signed, short-lived tokens bound to one user and one run.

Verifying a pass needs no database or Redis lookup. A pass is useless without the matching
user's own access token, and an access or refresh token can never be accepted as a pass because
passes are signed with their own salt.
"""

from django.core import signing

SALT = "railrush.waitroom.pass.v1"
MAX_TOKEN_LENGTH = 1024


def issue_pass(user_id, run_id, expires_at):
    claims = {"u": str(user_id), "r": int(run_id), "e": int(expires_at)}
    return signing.dumps(claims, salt=SALT, compress=False)


def verify_pass(token, user_id, run_id, at):
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return False
    try:
        claims = signing.loads(token, salt=SALT)
        return bool(
            claims.get("u") == str(user_id)
            and claims.get("r") == int(run_id)
            and claims.get("e", 0) > at
        )
    except (signing.BadSignature, AttributeError, TypeError, ValueError):
        return False
