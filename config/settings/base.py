# ruff: noqa: ERA001, E501
"""Base settings to build other settings files upon."""

import ssl
from datetime import timedelta
from pathlib import Path

import environ
from corsheaders.defaults import default_headers

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
# momcare_platform/
APPS_DIR = BASE_DIR / "momcare_platform"
env = environ.Env()

_env_file = BASE_DIR / ".env"
READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=_env_file.exists())
if READ_DOT_ENV_FILE:
    # OS environment variables take precedence over variables from .env
    env.read_env(str(_env_file))

# GENERAL
# ------------------------------------------------------------------------------
DEBUG = env.bool("DJANGO_DEBUG", False)
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True
LOCALE_PATHS = [str(BASE_DIR / "locale")]

# DATABASES
# ------------------------------------------------------------------------------
DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["ATOMIC_REQUESTS"] = True

# URLS
# ------------------------------------------------------------------------------
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
    "django.forms",
]
THIRD_PARTY_APPS = [
    "django_celery_beat",
    "rest_framework",
    "rest_framework.authtoken",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "drf_spectacular",
]

LOCAL_APPS = [
    # Foundational core apps (core/ is a namespace folder, not a single app).
    # NOTE: core.common is NOT listed here — it has no models needing migrations,
    # it's a shared library other apps import from directly, same as Neuro_RPM.
    "momcare_platform.core.organization",
    "momcare_platform.core.users",
    "momcare_platform.core.locations",
    "momcare_platform.core.staff",
    "momcare_platform.core.patients",
    "momcare_platform.core.monitoring",
    "momcare_platform.core.alerts",
    # No feature modules yet — the first one (momcare_platform.modules.<name>)
    # gets added here once it exists, following the Section 6 self-registration
    # pattern from the blueprint.
]
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# AUTHENTICATION
# ------------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
]
AUTH_USER_MODEL = "users.User"
# Platform role codes — the stable keys stored on ``users.Role.code`` and matched
# by the DRF permission classes in ``momcare_platform.core.common.permissions``.
# Two-tier: PLATFORM_ADMIN sees across hospitals; the rest belong to one hospital.
ROLE_PLATFORM_ADMIN = "platform_admin"
ROLE_HOSPITAL_ADMIN = "hospital_admin"
ROLE_PROVIDER = "provider"
ROLE_NURSE = "nurse"
ROLE_CARE_MANAGER = "care_manager"
ROLE_PATIENT = "patient"
# API-only backend: only the Django admin / Swagger use a browser login, and the
# admin ships its own login view. Point LOGIN_URL at it for any login_required.
LOGIN_URL = "admin:login"

# PASSWORDS
# ------------------------------------------------------------------------------
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # CORS must run before CommonMiddleware so credentialed cross-origin
    # requests (the SPA + HttpOnly refresh cookie) get the right headers.
    "corsheaders.middleware.CorsMiddleware",
    "momcare_platform.core.common.middleware.AuditLogMiddleware",
    "momcare_platform.core.common.middleware.DisplayTimezoneMiddleware",
    "momcare_platform.core.common.middleware.RequestLoggingMiddleware",
    "momcare_platform.core.common.middleware.NoCacheAPIMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
STATIC_ROOT = str(BASE_DIR / "staticfiles")
STATIC_URL = "/static/"
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# MEDIA
# ------------------------------------------------------------------------------
MEDIA_ROOT = str(APPS_DIR / "media")
MEDIA_URL = "/media/"

# TEMPLATES
# ------------------------------------------------------------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# FIXTURES
# ------------------------------------------------------------------------------
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# SECURITY
# ------------------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"

# EMAIL
# ------------------------------------------------------------------------------
EMAIL_BACKEND = env("DJANGO_EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("DJANGO_EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("DJANGO_EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("DJANGO_EMAIL_USE_TLS", default=True)
EMAIL_USE_SSL = env.bool("DJANGO_EMAIL_USE_SSL", default=False)
EMAIL_HOST_USER = env("DJANGO_EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_HOST_PASSWORD", default="")
EMAIL_TIMEOUT = 5
DEFAULT_FROM_EMAIL = env("DJANGO_DEFAULT_FROM_EMAIL", default="MomCare <noreply@momcare.example>")
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = "admin/"
ADMINS = ['"MomCare" <admin@momcare.example>']
MANAGERS = ADMINS

# LOGGING
# ------------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "formatters": {
        "json": {
            "()": "momcare_platform.core.common.request_logging.JsonFormatter",
            "format": "%(message)s",
        },
    },
    "handlers": {
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "django.db.backends": {"level": "ERROR", "handlers": ["console"], "propagate": False},
        "sentry_sdk": {"level": "ERROR", "handlers": ["console"], "propagate": False},
        "django.security.DisallowedHost": {"level": "ERROR", "handlers": ["console"], "propagate": False},
        "django.request": {"level": "ERROR", "handlers": ["console"], "propagate": False},
        "django": {"level": "INFO", "handlers": ["console"], "propagate": False},
        "django.server": {"level": "INFO", "handlers": ["console"], "propagate": False},
    },
}

REDIS_URL = env("REDIS_URL", default="redis://redis:6379/0")
REDIS_SSL = REDIS_URL.startswith("rediss://")

# Celery
# ------------------------------------------------------------------------------
if USE_TZ:
    CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_URL = REDIS_URL
CELERY_BROKER_USE_SSL = {"ssl_cert_reqs": ssl.CERT_NONE} if REDIS_SSL else None
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_REDIS_BACKEND_USE_SSL = CELERY_BROKER_USE_SSL
CELERY_RESULT_EXTENDED = True
CELERY_RESULT_BACKEND_ALWAYS_RETRY = True
CELERY_RESULT_BACKEND_MAX_RETRIES = 10
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
# TODO: set to whatever value is adequate in your circumstances
CELERY_TASK_TIME_LIMIT = 5 * 60
# TODO: set to whatever value is adequate in your circumstances
CELERY_TASK_SOFT_TIME_LIMIT = 60
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_WORKER_SEND_TASK_EVENTS = True
CELERY_TASK_SEND_SENT_EVENT = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

# django-rest-framework
# -------------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # JWT bearer tokens are the primary API auth. Session auth is kept for the
        # Django admin and the browsable API / Swagger docs only.
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    # How many proxies sit in front of the app. Without this DRF keys throttles
    # on the whole X-Forwarded-For string, which a client can forge to obtain a
    # fresh bucket on every request — defeating the login rate limit below.
    # 1 for a single platform load balancer; raise it if a CDN is added.
    "NUM_PROXIES": env.int("DJANGO_NUM_PROXIES", default=None),
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "100/day",
        "user": "5000/hour",
        "auth_sensitive": "5/min",
    },
    "EXCEPTION_HANDLER": "momcare_platform.core.common.exceptions.drf_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "momcare_platform.core.common.pagination.DefaultPagination",
}

# drf-spectacular
# -------------------------------------------------------------------------------
SPECTACULAR_SETTINGS = {
    "TITLE": "MomCare Backend API",
    "DESCRIPTION": "Modular maternal-health software suite",
    "VERSION": "1.0.0",
    # Restrict /api/docs/ to admin users
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAdminUser"],
}

# JWT authentication (djangorestframework-simplejwt)
# -------------------------------------------------------------------------------
# Access token rides in the Authorization header (app state only on the client).
# The refresh token is delivered in an HttpOnly cookie and never exposed to JS.
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=1),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}
REMEMBER_ME_DAYS = env.int("DJANGO_REMEMBER_ME_DAYS", default=30)

# Refresh-token cookie
# -------------------------------------------------------------------------------
REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/api/auth"
REFRESH_COOKIE_SAMESITE = env("DJANGO_REFRESH_COOKIE_SAMESITE", default="Lax")
REFRESH_COOKIE_SECURE = env.bool("DJANGO_REFRESH_COOKIE_SECURE", default=True)
REFRESH_COOKIE_DOMAIN = env("DJANGO_REFRESH_COOKIE_DOMAIN", default=None)

# Frontend origin — used to build links that are opened in the SPA, not the API
# (invitation acceptance, password reset). Must not be derived from the request:
# an attacker-supplied Host header would otherwise end up inside an email.
# -------------------------------------------------------------------------------
FRONTEND_URL = env("DJANGO_FRONTEND_URL", default="http://localhost:3000")

# CORS / CSRF — required for the SPA to send the credentialed refresh cookie.
# -------------------------------------------------------------------------------
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = env.list(
    "DJANGO_CORS_ALLOWED_ORIGINS",
    default=["http://localhost:3000", "http://127.0.0.1:3000"],
)
CORS_ALLOW_HEADERS = [*default_headers, "ngrok-skip-browser-warning"]
CSRF_TRUSTED_ORIGINS = env.list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default=CORS_ALLOWED_ORIGINS,
)

# Password Reset
# ------------------------------------------------------------------------------
PASSWORD_RESET_TIMEOUT = 3600
# NOTE: the reset link uses FRONTEND_URL, defined once above. It was redefined
# here with a stale :5173 default, which silently won and sent every invitation
# email to a port nothing runs on. Define it in one place only.
