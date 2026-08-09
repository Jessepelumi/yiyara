from .settings import *  # noqa: F403

SECRET_KEY = "test-only-secret-key-at-least-32-bytes-long"
INTERNAL_AUTH_SECRET = "test-only-internal-secret"
SIMPLE_JWT = {**SIMPLE_JWT, "SIGNING_KEY": SECRET_KEY}  # noqa: F405

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
