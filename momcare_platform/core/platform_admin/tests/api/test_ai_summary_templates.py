"""Platform-tier summary templates -- list/create/activate/deactivate under
/api/platform-admin/ai-config/summary-templates/. ROLE_PLATFORM_ADMIN only,
same gate as the instruction-preset endpoints this mirrors."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "/api/platform-admin/ai-config/summary-templates/"


def _full_sections(label="All Fields"):
    return [{"label": label, "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="templates-root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
        is_email_verified=True,
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "templates-root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def test_platform_admin_can_create_a_template(client, platform_admin_auth):
    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Baseline", "sections": _full_sections()}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    template = AISummaryTemplate.objects.get()
    assert template.organization is None
    assert template.sections == _full_sections()
    assert template.is_active is False


def test_organization_in_the_request_body_is_ignored(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Template Spoof Hospital")

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps(
            {"name": "Sneaky", "sections": _full_sections(), "organization": str(hospital.org.id)},
        ),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    assert AISummaryTemplate.objects.get().organization is None


def test_list_only_returns_platform_tier_templates(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Template List Isolation Hospital")
    AISummaryTemplate.objects.create(organization=None, name="Platform", sections=_full_sections())
    AISummaryTemplate.objects.create(organization=hospital.org, name="Org", sections=_full_sections())

    response = client.get(TEMPLATES_URL, **platform_admin_auth)

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Platform"]


def test_activate_deactivates_the_previous_active_platform_template(client, platform_admin_auth):
    old = AISummaryTemplate.objects.create(
        organization=None,
        name="Old",
        sections=_full_sections(),
        is_active=True,
    )
    new = AISummaryTemplate.objects.create(organization=None, name="New", sections=_full_sections())

    response = client.post(f"{TEMPLATES_URL}{new.id}/activate/", **platform_admin_auth)

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_activating_an_already_active_template_is_a_400(client, platform_admin_auth):
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        sections=_full_sections(),
        is_active=True,
    )

    response = client.post(f"{TEMPLATES_URL}{template.id}/activate/", **platform_admin_auth)

    assert response.status_code == 400


def test_deactivate_clears_is_active(client, platform_admin_auth):
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        sections=_full_sections(),
        is_active=True,
    )

    response = client.post(f"{TEMPLATES_URL}{template.id}/deactivate/", **platform_admin_auth)

    assert response.status_code == 200
    template.refresh_from_db()
    assert template.is_active is False


def test_activating_an_organizations_template_via_the_platform_endpoint_is_404(
    client, platform_admin_auth, make_hospital
):
    hospital = make_hospital("Template Cross Tier Hospital")
    template = AISummaryTemplate.objects.create(organization=hospital.org, name="Org", sections=_full_sections())

    response = client.post(f"{TEMPLATES_URL}{template.id}/activate/", **platform_admin_auth)

    assert response.status_code == 404


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Template Platform Endpoint Refusal Hospital")

    response = client.get(TEMPLATES_URL, **auth(hospital.admin.email))

    assert response.status_code == 403


def test_a_blank_name_is_rejected(client, platform_admin_auth):
    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "", "sections": _full_sections()}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AISummaryTemplate.objects.exists()


def test_sections_missing_a_field_is_rejected(client, platform_admin_auth):
    incomplete = [{"label": "Incomplete", "fields": [f for f in TEMPLATE_FIELD_VOCABULARY if f != "patient_name"]}]

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Baseline", "sections": incomplete}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AISummaryTemplate.objects.exists()


def test_sections_with_an_unknown_field_is_rejected(client, platform_admin_auth):
    bad = [{"label": "Bad", "fields": ["patient_name", "made_up_field"]}]

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Baseline", "sections": bad}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AISummaryTemplate.objects.exists()


def test_sections_with_a_duplicated_field_is_rejected(client, platform_admin_auth):
    duplicated = [
        {"label": "A", "fields": list(TEMPLATE_FIELD_VOCABULARY)},
        {"label": "B", "fields": ["patient_name"]},
    ]

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Baseline", "sections": duplicated}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AISummaryTemplate.objects.exists()
