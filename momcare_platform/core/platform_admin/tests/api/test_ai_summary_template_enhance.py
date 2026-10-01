"""Platform-tier template review step --
POST /api/platform-admin/ai-config/summary-templates/enhance/.
ROLE_PLATFORM_ADMIN only. Missing fields come back as an alert; a complete
template gets the entire summary previewed from sample data. Stateless:
nothing is created by calling this."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

ENHANCE_URL = "/api/platform-admin/ai-config/summary-templates/enhance/"


def _full_sections(label="All Fields"):
    return [{"label": label, "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="enhance-root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
        is_email_verified=True,
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "enhance-root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def _post(client, auth, payload):
    return client.post(ENHANCE_URL, data=json.dumps(payload), content_type="application/json", **auth)


def test_a_complete_template_returns_the_full_preview(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="A sample preview."):
        response = _post(client, platform_admin_auth, {"sections": _full_sections()})

    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is True
    assert body["missing_fields"] == []
    assert body["preview_text"] == "A sample preview."
    assert body["word_limit"] == 150
    assert body["sections"] == _full_sections()


def test_an_incomplete_template_alerts_with_the_missing_fields(client, platform_admin_auth):
    partial = [{"label": "Partial", "fields": ["patient_name"]}]

    with patch("momcare_platform.core.ai.openrouter_client.generate") as gen:
        response = _post(client, platform_admin_auth, {"sections": partial})

    gen.assert_not_called()
    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is False
    assert body["preview_text"] is None
    assert "patient_name" not in body["missing_fields"]
    assert len(body["missing_fields"]) == len(TEMPLATE_FIELD_VOCABULARY) - 1
    assert body["message"]
    assert body["sections"] == partial


def test_an_unknown_field_is_still_a_400(client, platform_admin_auth):
    response = _post(client, platform_admin_auth, {"sections": [{"label": "Bad", "fields": ["made_up_field"]}]})

    assert response.status_code == 400


def test_nothing_is_saved_by_calling_it(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Preview."):
        _post(client, platform_admin_auth, {"sections": _full_sections()})

    assert not AISummaryTemplate.objects.exists()


def test_returns_503_when_the_preview_call_fails(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = _post(client, platform_admin_auth, {"sections": _full_sections()})

    assert response.status_code == 503


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Enhance Platform Endpoint Refusal Hospital")

    response = _post(client, auth(hospital.admin.email), {"sections": _full_sections()})

    assert response.status_code == 403
