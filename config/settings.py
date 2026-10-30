import os

from dotenv import load_dotenv
from pathlib import Path
from datetime import timedelta
from django.core.exceptions import ImproperlyConfigured


load_dotenv()


def env_list(name, default):
    value = os.getenv(name)

    if not value:
        return tuple(default)
    return tuple(
        item.strip() 
        for item in value.split(",") 
        if item.strip()
    )


def positive_int(name, default):
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ImproperlyConfigured(f'{name} must be positive.')
    return value


# Product limits apply per proposal, never to an entire ambition.
PLAN_MAX_GOALS_PER_PROPOSAL = positive_int('PLAN_MAX_GOALS_PER_PROPOSAL', 3)
PLAN_MAX_TASKS_PER_PROPOSAL = positive_int('PLAN_MAX_TASKS_PER_PROPOSAL', 10)
PLAN_MAX_CHANGE_OPERATIONS = positive_int('PLAN_MAX_CHANGE_OPERATIONS', 25)
PLAN_MAX_CLARIFICATION_CHANGES = positive_int('PLAN_MAX_CLARIFICATION_CHANGES', 10)
if PLAN_MAX_CLARIFICATION_CHANGES > PLAN_MAX_CHANGE_OPERATIONS:
    raise ImproperlyConfigured('Clarification changes must not exceed the operation limit.')
PLAN_NEAR_TERM_DAYS = positive_int('PLAN_NEAR_TERM_DAYS', 7)
MIN_SESSION_MINUTES = positive_int('MIN_SESSION_MINUTES', 5)
MAX_SESSION_MINUTES = positive_int('MAX_SESSION_MINUTES', 480)
SCHEDULE_MAX_RANGE_DAYS = positive_int('SCHEDULE_MAX_RANGE_DAYS', 31)
PROPOSAL_TTL_SECONDS = positive_int('PROPOSAL_TTL_SECONDS', 86400)
CALENDAR_APPROVAL_LEASE_SECONDS = positive_int('CALENDAR_APPROVAL_LEASE_SECONDS', 120)
CONVERSATION_HISTORY_LIMIT = positive_int('CONVERSATION_HISTORY_LIMIT', 10)
OUTCOME_HISTORY_LIMIT = positive_int('OUTCOME_HISTORY_LIMIT', 50)
PLAN_MAX_QUESTIONS_PER_TURN = positive_int('PLAN_MAX_QUESTIONS_PER_TURN', 2)
PLAN_MAX_OPEN_QUESTIONS = positive_int('PLAN_MAX_OPEN_QUESTIONS', 10)
PLAN_MAX_QUESTIONS = positive_int('PLAN_MAX_QUESTIONS', 100)
GOOGLE_REQUEST_TIMEOUT_SECONDS = positive_int('GOOGLE_REQUEST_TIMEOUT_SECONDS', 20)
if MIN_SESSION_MINUTES > MAX_SESSION_MINUTES:
    raise ImproperlyConfigured('MIN_SESSION_MINUTES must not exceed MAX_SESSION_MINUTES.')


BASE_DIR = Path(__file__).resolve().parent.parent


# Security
SECRET_KEY = os.getenv('SECRET_KEY')

DEBUG = os.getenv("DEBUG", "False").lower() == "true"

ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    ("backend", "localhost", "127.0.0.1", "0.0.0.0"),
)


# Google authentication
GOOGLE_CLIENT_IDS = env_list("GOOGLE_CLIENT_IDS", ())

# Google Calendar authorization is separate from sign-in.
GOOGLE_CALENDAR_CLIENT_ID = os.getenv("GOOGLE_CALENDAR_CLIENT_ID", "")
GOOGLE_CALENDAR_CLIENT_SECRET = os.getenv("GOOGLE_CALENDAR_CLIENT_SECRET", "")
GOOGLE_CALENDAR_REDIRECT_URI = os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "")
GOOGLE_CALENDAR_ENCRYPTION_KEY = os.getenv("GOOGLE_CALENDAR_ENCRYPTION_KEY", "")


# Applications
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third-party applications
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",

    # Project applications
    "users",
    "goals",
    "tasks",
    "conversations",
    "calendars",
]


# Custom user model
AUTH_USER_MODEL = 'users.User'

AUTHENTICATION_BACKENDS = ['django.contrib.auth.backends.ModelBackend']


# Middleware
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# CORS & CSRF
CORS_ALLOWED_ORIGINS = env_list('CORS_ALLOWED_ORIGINS', ('http://localhost:3000', 'http://127.0.0.1:3000'))
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = env_list('CSRF_TRUSTED_ORIGINS', CORS_ALLOWED_ORIGINS)
FRONTEND_URL = os.getenv('FRONTEND_URL', '')
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'false' if DEBUG else 'true').lower() == 'true'
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '0'))
if os.getenv('TRUST_PROXY_HTTPS', 'false').lower() == 'true':
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')


# URLs and templates
ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.getenv("PGHOST"),
        "NAME": os.getenv("PGDATABASE"),
        "USER": os.getenv("PGUSER"),
        "PASSWORD": os.getenv("PGPASSWORD"),
        "PORT": os.getenv("PGPORT", 5432),
        "OPTIONS": {
            "sslmode": os.getenv("PGSSLMODE", "require"),
        },
        "DISABLE_SERVER_SIDE_CURSORS": True,
        "CONN_HEALTH_CHECKS": True,
    }
}


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "UserAttributeSimilarityValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "MinimumLengthValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "CommonPasswordValidator"
        ),
    },
    {
        "NAME": (
            "django.contrib.auth.password_validation."
            "NumericPasswordValidator"
        ),
    },
]


# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "auth_google": "10/min",
        "auth_refresh": "20/min",
        "auth_logout": "20/min",
        "calendar": "30/min",
        "task_planning": os.getenv("TASK_PLANNING_RATE", "10/min"),
        "plan_chat": os.getenv("PLAN_CHAT_RATE", "10/min"),
    },
}

# JWT authentication
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),

    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,

    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,

    "AUTH_HEADER_TYPES": ("Bearer",),

    "TOKEN_REFRESH_SERIALIZER": (
        "users.serializers.ActiveUserTokenRefreshSerializer"
    ),
}


# AI provider routing
AI_REQUEST_TIMEOUT_SECONDS = float(os.getenv("AI_REQUEST_TIMEOUT_SECONDS", "30"))
AI_TOTAL_TIMEOUT_SECONDS = float(os.getenv("AI_TOTAL_TIMEOUT_SECONDS", "45"))
AI_RETRY_MAX_ATTEMPTS = int(os.getenv("AI_RETRY_MAX_ATTEMPTS", "2"))
AI_RETRY_MIN_SECONDS = float(os.getenv("AI_RETRY_MIN_SECONDS", "0.5"))
AI_RETRY_MAX_SECONDS = float(os.getenv("AI_RETRY_MAX_SECONDS", "4"))
AI_REQUEST_LEASE_SECONDS = positive_int('AI_REQUEST_LEASE_SECONDS', max(120, int(AI_TOTAL_TIMEOUT_SECONDS) + 30))
if AI_REQUEST_LEASE_SECONDS <= AI_TOTAL_TIMEOUT_SECONDS:
    raise ImproperlyConfigured('AI_REQUEST_LEASE_SECONDS must exceed the total AI timeout.')
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "4096"))

AI_PROVIDER_ORDER = env_list(
    "AI_PROVIDER_ORDER",
    ("gemini",),
)

AI_ROUTES = {
    "task_planning": env_list("AI_TASK_PLANNING_PROVIDERS", AI_PROVIDER_ORDER),
    "plan_iteration": env_list(
        "AI_PLAN_ITERATION_PROVIDERS",
        AI_PROVIDER_ORDER,
    ),
}


_openrouter_headers = {
    key: value
    for key, value in {
        "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL"),
        "X-Title": os.getenv(
            "OPENROUTER_APP_NAME",
            "Yiyara",
        ),
    }.items()
    if value
}

AI_PROVIDERS = {
    "gemini": {
        "BACKEND": "ai.providers.gemini_provider.GeminiProvider",
        "OPTIONS": {
            "api_key": os.getenv("GEMINI_API_KEY"),
            "model": os.getenv(
                "GEMINI_MODEL",
                "gemini-2.5-flash-lite",
            ),
            "timeout_seconds": AI_REQUEST_TIMEOUT_SECONDS,
        },
    },
}


# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files
STATIC_URL = "static/"

# OAuth callback query strings contain temporary credentials.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "calendar_callback": {"()": "calendars.logging.RedactCalendarCallback"},
    },
    "loggers": {
        "django.server": {"filters": ["calendar_callback"]},
        "django.request": {"filters": ["calendar_callback"]},
    },
}
