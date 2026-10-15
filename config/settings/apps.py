"""App wiring. Each phase that adds an app re-writes this small file."""

LOCAL_APPS = [
    "apps.accounts",
]

AUTH_USER_MODEL = "accounts.User"
