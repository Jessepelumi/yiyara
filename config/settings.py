import os

from dotenv import load_dotenv
from pathlib import Path
from datetime import timedelta


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


BASE_DIR = Path(__file__).resolve().parent.parent


# Security
SECRET_KEY = os.getenv('SECRET_KEY')

DEBUG = os.getenv("DEBUG", "True").lower() == "true"

ALLOWED_HOSTS = env_list(
    "ALLOWED_HOSTS",
    ("backend", "localhost", "127.0.0.1", "0.0.0.0"),
)


# Google authentication
GOOGLE_CLIENT_IDS = env_list("GOOGLE_CLIENT_IDS", ())


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
CORS_ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]


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
            "sslmode": "require",
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
        "goal_preview": "10/hour",
        "auth_google": "10/min",
        "auth_refresh": "20/min",
        "auth_logout": "20/min",
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
AI_MAX_OUTPUT_TOKENS = int(os.getenv("AI_MAX_OUTPUT_TOKENS", "4096"))

AI_PROVIDER_ORDER = env_list(
    "AI_PROVIDER_ORDER",
    ("gemini",),
)

AI_ROUTES = {
    "goal_decomposition": env_list(
        "AI_DECOMPOSITION_PROVIDERS",
        AI_PROVIDER_ORDER,
    ),
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
