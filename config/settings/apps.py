"""App wiring. Each phase that adds an app re-writes this small file."""

LOCAL_APPS = [
    "apps.common",
    "apps.accounts",
    "apps.catalog",
]

AUTH_USER_MODEL = "accounts.User"
