"""Shared pytest fixtures.

The hospital/staff factories here exist because almost every access-control
test needs at least two tenants: a rule that looks correct against one hospital
tells you nothing about isolation.
"""

from types import SimpleNamespace

import pytest
from django.conf import settings

from momcare_platform.core.organization.models import Organization
from momcare_platform.core.staff.models import Staff
from momcare_platform.core.users.models import Role, User

DEFAULT_PASSWORD = "TestPass!2026"


@pytest.fixture(autouse=True)
def _media_storage(settings, tmpdir) -> None:
    settings.MEDIA_ROOT = tmpdir.strpath


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
