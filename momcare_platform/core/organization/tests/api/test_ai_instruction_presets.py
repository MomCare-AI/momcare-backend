"""Organization-tier instruction presets -- list/create/activate/deactivate
under /api/organization/me/instruction-presets/. hospital_admin only, same
gate the old ai-instructions endpoint used."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AIInstructionPreset

pytestmark = pytest.mark.django_db

PRESETS_URL = "/api/organization/me/instruction-presets/"


def test_hospital_admin_can_create_a_preset(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Create Hospital")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Winter 2026", "content": "Emphasize hydration."}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    preset = AIInstructionPreset.objects.get()
    assert preset.organization_id == hospital.org.id


def test_organization_in_the_request_body_is_ignored(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Spoof Hospital")
    other_hospital = make_hospital("Org Preset Spoof Target Hospital")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Sneaky", "content": "Text.", "organization": str(other_hospital.org.id)}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert AIInstructionPreset.objects.get().organization_id == hospital.org.id


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Org Preset Provider Refusal Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@orgpresets.test")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Should not work", "content": "Text."}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403


def test_list_only_returns_this_hospitals_own_presets(client, make_hospital, auth):
    hospital = make_hospital("Org Preset List Isolation Hospital")
    other_hospital = make_hospital("Org Preset List Other Hospital")
    AIInstructionPreset.objects.create(organization=hospital.org, name="Mine", content="M.")
    AIInstructionPreset.objects.create(organization=other_hospital.org, name="Theirs", content="T.")
    AIInstructionPreset.objects.create(organization=None, name="Platform", content="P.")

    response = client.get(PRESETS_URL, **auth(hospital.admin.email))

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Mine"]


def test_activate_deactivates_the_previous_active_preset_for_this_hospital_only(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Activate Hospital")
    old = AIInstructionPreset.objects.create(
        organization=hospital.org,
        name="Old",
        content="Old.",
        is_active=True,
    )
    new = AIInstructionPreset.objects.create(organization=hospital.org, name="New", content="New.")

    response = client.post(f"{PRESETS_URL}{new.id}/activate/", **auth(hospital.admin.email))

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_another_hospitals_preset_id_is_404_not_403(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Cross Tenant Hospital")
    other_hospital = make_hospital("Org Preset Cross Tenant Other Hospital")
    preset = AIInstructionPreset.objects.create(organization=other_hospital.org, name="Theirs", content="T.")

    response = client.post(f"{PRESETS_URL}{preset.id}/activate/", **auth(hospital.admin.email))

    assert response.status_code == 404


def test_a_platform_tier_preset_id_is_404_via_the_organization_endpoint(client, make_hospital, auth):
    hospital = make_hospital("Org Preset Cross Tier Hospital")
    preset = AIInstructionPreset.objects.create(organization=None, name="Platform", content="P.")

    response = client.post(f"{PRESETS_URL}{preset.id}/deactivate/", **auth(hospital.admin.email))

    assert response.status_code == 404
