"""
With these settings, tests run faster.
"""

from .base import *  # noqa: F403
from .base import BASE_DIR, TEMPLATES, env

# Read .env file for local development (no Docker)
# ------------------------------------------------------------------------------
env.read_env(str(BASE_DIR / ".env"))

# GENERAL
# ------------------------------------------------------------------------------
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    default="!!!SET DJANGO_SECRET_KEY!!!",
)
TEST_RUNNER = "django.test.runner.DiscoverRunner"

# PASSWORDS
# ------------------------------------------------------------------------------
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# EMAIL
# ------------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# DEBUGGING FOR TEMPLATES
# ------------------------------------------------------------------------------
TEMPLATES[0]["OPTIONS"]["debug"] = True  # type: ignore[index]

# MEDIA
# ------------------------------------------------------------------------------
MEDIA_URL = "http://media.testserver/"

# Celery
# ------------------------------------------------------------------------------
CELERY_TASK_ALWAYS_EAGER = True
# Care plans are written inside the request here (a test or a dev server wants the result at once).
CARE_PLAN_GENERATE_IN_BACKGROUND = False
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_RESULT_BACKEND = "cache+memory://"

# NOTE: DATABASES is intentionally NOT overridden here — it stays whatever
# base.py resolved from DATABASE_URL (Postgres). Tests must run on the same
# database engine as production so Postgres-specific behavior (RLS policies,
# JSONField operators, select_for_update()) is actually exercised.
