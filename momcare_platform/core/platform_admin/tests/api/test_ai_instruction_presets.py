"""Platform-tier instruction presets -- list/create/activate/deactivate
under /api/platform-admin/ai-config/instruction-presets/. ROLE_PLATFORM_ADMIN
only, same gate as AIProviderConfigView."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

PRESETS_URL = "/api/platform-admin/ai-config/instruction-presets/"


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="presets-root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
        is_email_verified=True,
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "presets-root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def test_platform_admin_can_create_a_preset(client, platform_admin_auth):
    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Baseline", "content": "Always mention medication adherence."}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    preset = AIInstructionPreset.objects.get()
    assert preset.organization is None
    assert preset.content == "Always mention medication adherence."
    assert preset.is_active is False


def test_organization_in_the_request_body_is_ignored(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Preset Spoof Hospital")

    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Sneaky", "content": "Text.", "organization": str(hospital.org.id)}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    assert AIInstructionPreset.objects.get().organization is None


def test_list_only_returns_platform_tier_presets(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Preset List Isolation Hospital")
    AIInstructionPreset.objects.create(organization=None, name="Platform", content="P.")
    AIInstructionPreset.objects.create(organization=hospital.org, name="Org", content="O.")

    response = client.get(PRESETS_URL, **platform_admin_auth)

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Platform"]


def test_activate_deactivates_the_previous_active_platform_preset(client, platform_admin_auth):
    old = AIInstructionPreset.objects.create(
        organization=None,
        name="Old",
        content="Old.",
        is_active=True,
    )
    new = AIInstructionPreset.objects.create(organization=None, name="New", content="New.")

    response = client.post(f"{PRESETS_URL}{new.id}/activate/", **platform_admin_auth)

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_activating_an_already_active_preset_is_a_400(client, platform_admin_auth):
    preset = AIInstructionPreset.objects.create(
        organization=None,
        name="Active",
        content="Text.",
        is_active=True,
    )

    response = client.post(f"{PRESETS_URL}{preset.id}/activate/", **platform_admin_auth)

    assert response.status_code == 400


def test_deactivate_clears_is_active(client, platform_admin_auth):
    preset = AIInstructionPreset.objects.create(
        organization=None,
        name="Active",
        content="Text.",
        is_active=True,
    )

    response = client.post(f"{PRESETS_URL}{preset.id}/deactivate/", **platform_admin_auth)

    assert response.status_code == 200
    preset.refresh_from_db()
    assert preset.is_active is False


def test_activating_an_organizations_preset_via_the_platform_endpoint_is_404(
    client, platform_admin_auth, make_hospital
):
    hospital = make_hospital("Preset Cross Tier Hospital")
    preset = AIInstructionPreset.objects.create(organization=hospital.org, name="Org", content="O.")

    response = client.post(f"{PRESETS_URL}{preset.id}/activate/", **platform_admin_auth)

    assert response.status_code == 404


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Preset Platform Endpoint Refusal Hospital")

    response = client.get(PRESETS_URL, **auth(hospital.admin.email))

    assert response.status_code == 403


def test_a_blank_name_is_rejected(client, platform_admin_auth):
    """Review Focus item: name/content have no blank=True on the model, so
    DRF's ModelSerializer should already reject an empty one -- pinned here
    so a future change to the model's field options can't silently make a
    nameless preset possible."""
    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "", "content": "Text."}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AIInstructionPreset.objects.exists()


def test_blank_content_is_rejected(client, platform_admin_auth):
    response = client.post(
        PRESETS_URL,
        data=json.dumps({"name": "Baseline", "content": ""}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AIInstructionPreset.objects.exists()
