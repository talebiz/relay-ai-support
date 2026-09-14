"""
Django settings for the Relay backend.

Configuration values (secret key, database credentials, allowed hosts)
are read from environment variables so the same code runs unchanged
inside Docker Compose (see docker-compose.yml) and, if needed, locally
with a plain venv + a local .env file.
"""

import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load variables from a local .env file if present. Inside Docker,
# env_file in docker-compose.yml already injects these, so this is a
# no-op there; it only matters for local (non-Docker) development.
load_dotenv(BASE_DIR.parent / ".env")

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Core security settings ---

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")

# DEBUG defaults to False; must be explicitly enabled via env var.
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"

ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]


# --- Application definition ---

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    # Local apps
    "health",
    "accounts",
    "orders",
    # Part 4: conversation/message persistence - the system of record
    # for the chat (see conversations/models.py docstring for why this
    # is a separate app rather than folded into orders or support).
    "conversations",
    # Part 3: event-driven architecture (RabbitMQ). See each app's
    # docstrings for what it owns:
    #   - messaging:  shared envelope, topology, publisher, idempotency model
    #   - support:    HTTP entry point that turns a request into a command
    #   - streaming:  SSE view that turns RabbitMQ events into a browser stream
    "messaging",
    "support",
    "streaming",
]

# Custom user model. See accounts/models.py for why this exists even
# though it only adds a `role` field today: swapping user models after
# migrations already reference auth.User is painful, so we do it once,
# up front, rather than retrofitting it when the RBAC bonus is tackled.
AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # CorsMiddleware must be placed as high as possible, and before
    # CommonMiddleware, since the React frontend runs on a different
    # origin (e.g. http://localhost:5173) than the backend.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"


# --- Database ---
# Read connection details from environment variables. DB_HOST is set to
# "postgres" inside docker-compose.yml (the service name), not "localhost",
# because each container has its own network namespace - "localhost"
# inside this container would mean this container, not the postgres one.

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "relay"),
        "USER": os.getenv("POSTGRES_USER", "relay_user"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("DB_HOST", "localhost"),
        "PORT": os.getenv("DB_PORT", "5432"),
    }
}


# --- Password validation ---

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --- Internationalization ---

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# --- Static files ---

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Django REST Framework ---

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    # Deny-by-default: every view must explicitly opt into anything
    # looser than "you must be logged in". This is the DRF-level half
    # of "authorization lives in code" - nothing is world-readable by
    # accident just because a view forgot to set permission_classes.
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
}

# --- JWT (djangorestframework-simplejwt) ---
# Short-lived access tokens + refresh tokens is the standard tradeoff
# for an SPA talking to a separate API origin (no shared session cookie
# domain to lean on). The AI worker never issues or verifies tokens
# itself - Django is the only component that authenticates a user; the
# worker only ever receives an already-resolved user id inside a
# command message (see messaging/envelope.py, part 3).
#
# Part 4 adds one more consumer of these tokens: streaming/views.py
# validates an access token passed as a query parameter (?token=...)
# before opening an SSE connection, since EventSource cannot send a
# custom Authorization header. It reuses this exact same JWT config -
# no separate "streaming token" scheme was introduced.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}


# --- CORS ---
# The React dev server (Vite) runs on a different origin than the
# backend, so the browser needs explicit CORS permission to call the API.

CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]


# --- RabbitMQ (part 3: event-driven architecture) ---
# Connection details are read directly from env vars by
# messaging/publisher.py and streaming/views.py rather than duplicated
# here, since neither pika nor aio-pika hook into Django's settings
# system the way DATABASES does. Kept as plain os.getenv calls at the
# point of use - see those modules.
