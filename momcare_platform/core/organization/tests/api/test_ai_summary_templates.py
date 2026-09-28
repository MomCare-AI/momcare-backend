"""Organization-tier summary templates -- list/create/activate/deactivate
under /api/organization/me/summary-templates/. hospital_admin only, same
gate the organization-tier instruction presets use."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "/api/organization/me/summary-templates/"


def _full_sections(label="All Fields"):
    return [{"label": label, "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_hospital_admin_can_create_a_template(client, make_hospital, auth):
    hospital = make_hospital("Org Template Create Hospital")

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Name First", "sections": _full_sections()}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    template = AISummaryTemplate.objects.get()
    assert template.organization_id == hospital.org.id


def test_organization_in_the_request_body_is_ignored(client, make_hospital, auth):
    hospital = make_hospital("Org Template Spoof Hospital")
    other_hospital = make_hospital("Org Template Spoof Target Hospital")

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps(
            {"name": "Sneaky", "sections": _full_sections(), "organization": str(other_hospital.org.id)},
        ),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert AISummaryTemplate.objects.get().organization_id == hospital.org.id


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Org Template Provider Refusal Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@orgtemplates.test")

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Should not work", "sections": _full_sections()}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403


def test_list_only_returns_this_hospitals_own_templates(client, make_hospital, auth):
    hospital = make_hospital("Org Template List Isolation Hospital")
    other_hospital = make_hospital("Org Template List Other Hospital")
    AISummaryTemplate.objects.create(organization=hospital.org, name="Mine", sections=_full_sections())
    AISummaryTemplate.objects.create(organization=other_hospital.org, name="Theirs", sections=_full_sections())
    AISummaryTemplate.objects.create(organization=None, name="Platform", sections=_full_sections())

    response = client.get(TEMPLATES_URL, **auth(hospital.admin.email))

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Mine"]


def test_activate_deactivates_the_previous_active_template_for_this_hospital_only(client, make_hospital, auth):
    hospital = make_hospital("Org Template Activate Hospital")
    old = AISummaryTemplate.objects.create(
        organization=hospital.org,
        name="Old",
        sections=_full_sections(),
        is_active=True,
    )
    new = AISummaryTemplate.objects.create(organization=hospital.org, name="New", sections=_full_sections())

    response = client.post(f"{TEMPLATES_URL}{new.id}/activate/", **auth(hospital.admin.email))

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_another_hospitals_template_id_is_404_not_403(client, make_hospital, auth):
    hospital = make_hospital("Org Template Cross Tenant Hospital")
    other_hospital = make_hospital("Org Template Cross Tenant Other Hospital")
    template = AISummaryTemplate.objects.create(organization=other_hospital.org, name="Theirs", sections=_full_sections())

    response = client.post(f"{TEMPLATES_URL}{template.id}/activate/", **auth(hospital.admin.email))

    assert response.status_code == 404


def test_a_platform_tier_template_id_is_404_via_the_organization_endpoint(client, make_hospital, auth):
    hospital = make_hospital("Org Template Cross Tier Hospital")
    template = AISummaryTemplate.objects.create(organization=None, name="Platform", sections=_full_sections())

    response = client.post(f"{TEMPLATES_URL}{template.id}/deactivate/", **auth(hospital.admin.email))

    assert response.status_code == 404
