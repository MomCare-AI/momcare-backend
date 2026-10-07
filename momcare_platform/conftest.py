"""Shared pytest fixtures.

The hospital/staff factories here exist because almost every access-control
test needs at least two tenants: a rule that looks correct against one hospital
tells you nothing about isolation.
"""

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core.cache import cache

from momcare_platform.core.organization.models import Organization
from momcare_platform.core.staff.models import Staff
from momcare_platform.core.users.models import Role, User

DEFAULT_PASSWORD = "TestPass!2026"


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


@pytest.fixture(autouse=True)
def _no_real_openrouter_calls() -> Generator[None]:
    """The AI Summary feature's enrollment trigger fires on every Patient
    creation, unconditionally -- so without this, every test in the whole
    suite that creates a patient (not just core.ai's own tests) would make a
    real outbound HTTP call, or crash on an unconfigured MagicMock response
    (see core/ai/tests/test_never_hits_real_network.py). A test that wants
    a specific return value patches the same target explicitly inside its
    own body -- that patch nests correctly on top of this one.

    ``generate_researched`` is the care plan's call, and it fires on every
    saved reading (plans are written inline under the test settings), so any
    test that records a reading -- vitals, alerts, patients -- would otherwise
    spend real API credit with the key from ``.env``. ``None`` is what a failed
    call returns, which the care plan already answers with its fixed baseline.
    """
    with (
        patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None),
        patch("momcare_platform.core.ai.openrouter_client.generate_researched", return_value=None),
        patch("momcare_platform.core.ai.openrouter_client.list_available_models", return_value=None),
    ):
        yield


@pytest.fixture(autouse=True)
def _reset_throttles() -> Generator[None]:
    """Give every test a fresh rate-limit budget.

    DRF keeps throttle counters in the default cache, keyed by client IP — and
    every test shares 127.0.0.1. Without this the suite shares one ``100/day``
    anon bucket, so tests start returning 429 once enough of them have logged
    in, and which ones fail depends on execution order rather than on the code.

    Clearing between tests keeps that from being a silent ceiling on how many
    tests this project can have.
    """
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_hospital(db):
    """Create an Organization plus its owning hospital_admin.

    Defaults to APPROVED because most tests are about what happens after the
    review gate; pass ``status`` to exercise the gate itself.
    """

    def _make(
        name: str,
        *,
        status: str = Organization.STATUS_APPROVED,
        admin_email: str | None = None,
        password: str = DEFAULT_PASSWORD,
    ):
        slug = "".join(ch for ch in name.lower() if ch.isalnum())
        admin = User.objects.create_user(
            email=admin_email or f"admin@{slug}.test",
            password=password,
            first_name="Admin",
            last_name=name.split()[0],
            role=Role.objects.get(code=settings.ROLE_HOSPITAL_ADMIN),
        )
        org = Organization.objects.create(
            name=name,
            owner=admin,
            status=status,
            email=f"info@{slug}.test",
            phone="0510000000",
            city="Islamabad",
            country="Pakistan",
        )
        admin.organization = org
        admin.save(update_fields=["organization", "updated_at"])
        return SimpleNamespace(org=org, admin=admin, password=password)

    return _make


@pytest.fixture
def make_staff(db):
    """Add a staff member in a given role to an existing hospital."""

    def _make(org, role_code: str, email: str, password: str = DEFAULT_PASSWORD):
        user = User.objects.create_user(
            email=email,
            password=password,
            first_name=role_code.split("_")[0].title(),
            last_name="User",
            role=Role.objects.get(code=role_code),
        )
        user.organization = org
        user.save(update_fields=["organization", "updated_at"])
        Staff.objects.create(user=user, employee_id=f"{email.split('@')[0][:12]}-EMP")
        return user

    return _make


@pytest.fixture
def auth(client):
    """Log in through the real endpoint and return Authorization headers.

    Goes through ``/api/auth/login/`` rather than minting a token directly, so
    the tenant gate is exercised on the way in.
    """

    def _auth(email: str, password: str = DEFAULT_PASSWORD) -> dict:
        response = client.post(
            "/api/auth/login/",
            data={"email": email, "password": password},
            content_type="application/json",
        )
        assert response.status_code == 200, f"login failed for {email}: {response.content}"
        return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}

    return _auth
