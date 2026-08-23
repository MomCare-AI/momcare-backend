# ruff: noqa: E501
import logging

from .base import *  # noqa: F403
from .base import DATABASES, REDIS_URL, env

# GENERAL
# ------------------------------------------------------------------------------
DEBUG = False  # never inherited: an env var or a stray .env must not be able to flip this
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["momcare.example"])

# DATABASES
# ------------------------------------------------------------------------------
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)

# CACHES
# ------------------------------------------------------------------------------
# The database, not Redis. Throttle counters live in the cache, and a
# per-process backend gives every Gunicorn worker its own copy — silently
# multiplying every rate limit by the worker count. A shared backend keeps
# them correct, and the database is one we already run.
#
# Requires `manage.py createcachetable` in the release step.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": env("DJANGO_CACHE_TABLE", default="django_cache_table"),
    },
}

# SECURITY
# ------------------------------------------------------------------------------
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
SESSION_COOKIE_NAME = "__Secure-sessionid"
CSRF_COOKIE_SECURE = True
CSRF_COOKIE_NAME = "__Secure-csrftoken"
# TODO: set this to 60 seconds first and then to 518400 once you prove the former works
SECURE_HSTS_SECONDS = 60
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=True)
SECURE_CONTENT_TYPE_NOSNIFF = env.bool("DJANGO_SECURE_CONTENT_TYPE_NOSNIFF", default=True)

# STATIC & MEDIA
# ------------------------------------------------------------------------------
# Oracle Object Storage — S3-compatible API, so this uses the same django-storages
# S3 backend AWS S3 would use, just pointed at Oracle's endpoint instead (Oracle's
# Always Free tier already includes object storage; no separate AWS account needed).
# https://django-storages.readthedocs.io/en/latest/backends/amazon-S3.html#settings
AWS_ACCESS_KEY_ID = env("DJANGO_STORAGE_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("DJANGO_STORAGE_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = env("DJANGO_STORAGE_BUCKET_NAME", default="")
AWS_S3_REGION_NAME = env("DJANGO_STORAGE_REGION_NAME", default="ap-singapore-1")
# Oracle's S3-compatible endpoint, e.g.:
# https://<namespace>.compat.objectstorage.ap-singapore-1.oraclecloud.com
AWS_S3_ENDPOINT_URL = env("DJANGO_STORAGE_ENDPOINT_URL", default="")
AWS_QUERYSTRING_AUTH = False
_STORAGE_EXPIRY = 60 * 60 * 24 * 7
AWS_S3_OBJECT_PARAMETERS = {
    "CacheControl": f"max-age={_STORAGE_EXPIRY}, s-maxage={_STORAGE_EXPIRY}, must-revalidate",
}

# Object storage is used when it is configured, and the app falls back to
# local disk when it is not. Without this the four settings above have no
# defaults, so the process refuses to start even on a deployment where nobody
# has uploaded a file yet — object storage becomes a prerequisite for booting
# rather than a feature. The fallback is loudly logged, because files written
# to a container filesystem do not survive a redeploy.
_OBJECT_STORAGE_CONFIGURED = all(
    [AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_STORAGE_BUCKET_NAME, AWS_S3_ENDPOINT_URL],
)

if _OBJECT_STORAGE_CONFIGURED:
    _default_storage = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {"location": "media", "file_overwrite": False},
    }
    MEDIA_URL = f"{AWS_S3_ENDPOINT_URL}/{AWS_STORAGE_BUCKET_NAME}/media/"
else:
    logging.getLogger(__name__).warning(
        "Object storage is not configured (DJANGO_STORAGE_*). Uploaded files will be "
        "written to local disk and WILL BE LOST on the next deploy. Configure it before "
        "any real upload.",
    )
    _default_storage = {"BACKEND": "django.core.files.storage.FileSystemStorage"}

STORAGES = {
    "default": _default_storage,
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# EMAIL
# ------------------------------------------------------------------------------
DEFAULT_FROM_EMAIL = env("DJANGO_DEFAULT_FROM_EMAIL", default="MomCare <noreply@momcare.example>")
SERVER_EMAIL = env("DJANGO_SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)
EMAIL_SUBJECT_PREFIX = env("DJANGO_EMAIL_SUBJECT_PREFIX", default="[MomCare] ")

# ADMIN
# ------------------------------------------------------------------------------
ADMIN_URL = env("DJANGO_ADMIN_URL")

# SMTP
# ------------------------------------------------------------------------------
# Env-driven, not hardcoded. Managed hosts often block outbound SMTP, and the
# symptom is a connection that times out rather than an error naming the
# cause - so the transport has to be switchable without a code change.
EMAIL_BACKEND = env(
    "DJANGO_EMAIL_BACKEND",
    default="django.core.mail.backends.smtp.EmailBackend",
)
EMAIL_HOST = env("DJANGO_EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("DJANGO_EMAIL_PORT", default=587)
EMAIL_USE_TLS = env.bool("DJANGO_EMAIL_USE_TLS", default=True)
EMAIL_HOST_USER = env("DJANGO_EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = env("DJANGO_EMAIL_HOST_PASSWORD")

# Sentry
# ------------------------------------------------------------------------------
# Optional: only initialized when a DSN is provided. Leave SENTRY_DSN unset to
# disable error reporting entirely. PHI/PII must never leave the system via
# error telemetry — send_default_pii stays False.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.integrations.redis import RedisIntegration

    SENTRY_LOG_LEVEL = env.int("DJANGO_SENTRY_LOG_LEVEL", logging.INFO)

    sentry_logging = LoggingIntegration(
        level=SENTRY_LOG_LEVEL,
        event_level=logging.ERROR,
    )
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[sentry_logging, DjangoIntegration(), CeleryIntegration(), RedisIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        send_default_pii=False,
    )
