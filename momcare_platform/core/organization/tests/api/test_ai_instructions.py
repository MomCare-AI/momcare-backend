"""PATCH /api/organization/me/ai-instructions/ -- the org-level AI Summary
steering text. hospital_admin only, same shape as confidence-threshold."""

import json

import pytest

pytestmark = pytest.mark.django_db

URL = "/api/organization/me/ai-instructions/"


def test_hospital_admin_can_set_the_instructions(client, make_hospital, auth):
    hospital = make_hospital("AI Instructions Hospital")

    response = client.patch(
        URL,
        data=json.dumps({"ai_custom_instructions": "Always mention medication adherence."}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    hospital.org.refresh_from_db()
    assert hospital.org.ai_custom_instructions == "Always mention medication adherence."


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    from django.conf import settings

    hospital = make_hospital("AI Instructions Provider Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@aiinstructions.test")

    response = client.patch(
        URL,
        data=json.dumps({"ai_custom_instructions": "Should not be allowed."}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403
