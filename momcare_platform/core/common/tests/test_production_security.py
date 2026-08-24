"""Production security settings.

These are the settings nobody notices when they are wrong. The site still
serves, the tests still pass, and the only symptom is that a protection quietly
is not there - so they are asserted rather than trusted.

Read from the module directly rather than from `django.conf.settings`: the test
suite runs on `config.settings.test`, and the point is what production sends.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

ONE_YEAR = 31536000


@pytest.fixture(scope="module")
def production():
    """Import config.settings.production with the variables it refuses to boot without."""
    required = {
        "DJANGO_SECRET_KEY": "test-secret-key-not-used-for-anything-real-0123456789",
        "DJANGO_ADMIN_URL": "test-admin/",
        "DJANGO_ALLOWED_HOSTS": "example.test",
        "DATABASE_URL": "postgres://u:p@localhost:5432/db",
        "DJANGO_EMAIL_HOST_USER": "resend",
        "DJANGO_EMAIL_HOST_PASSWORD": "re_not_a_real_key",
    }
    previous = {k: os.environ.get(k) for k in required}
    os.environ.update(required)
    # The module reads DJANGO_READ_DOT_ENV_FILE-driven state at import time;
    # a developer's own .env must not decide whether this test passes.
    os.environ["DJANGO_READ_DOT_ENV_FILE"] = "False"
    try:
        yield importlib.import_module("config.settings.production")
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_hsts_lasts_a_year_not_a_minute(production):
    """The template shipped 60 seconds as a temporary value to prove HTTPS worked.

    A browser told to remember for a minute has forgotten by the next visit,
    which leaves the first request of every session downgradeable - the attack
    the header exists to prevent.
    """
    assert production.SECURE_HSTS_SECONDS >= ONE_YEAR


def test_hsts_preload_is_not_claimed_below_the_threshold(production):
    """Preload lists reject any domain whose max-age is under a year.

    Sending `preload` with a shorter max-age is not a smaller protection, it is
    a claim the browser will not honour - and it reads as protection to anyone
    inspecting the headers.
    """
    if production.SECURE_HSTS_PRELOAD:
        assert production.SECURE_HSTS_SECONDS >= ONE_YEAR


def test_debug_is_off(production):
    """The single setting that turns every error page into a source listing."""
    assert production.DEBUG is False


def test_cookies_and_transport_are_secure(production):
    assert production.SECURE_SSL_REDIRECT is True
    assert production.SESSION_COOKIE_SECURE is True
    assert production.CSRF_COOKIE_SECURE is True
    assert production.SECURE_CONTENT_TYPE_NOSNIFF is True


def test_the_proxy_header_is_trusted_for_https(production):
    """Behind a load balancer Django only knows the scheme from this header.

    Without it every request looks like HTTP, so SECURE_SSL_REDIRECT sends the
    browser back to a URL that redirects again - an infinite loop rather than a
    security failure, but the site is down either way.
    """
    assert production.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


def test_hsts_is_configurable_without_editing_code():
    """A ratchet this consequential must be adjustable from the environment.

    Every other security setting beside it reads from env; this one was a
    literal, which is how it sat at a scaffold value long after deployment.
    """
    source = Path("config/settings/production.py").read_text(encoding="utf-8")
    assert "SECURE_HSTS_SECONDS = env.int(" in source
