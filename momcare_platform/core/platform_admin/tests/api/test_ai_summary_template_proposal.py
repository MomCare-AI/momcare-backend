"""Platform-tier AI-assisted template authoring --
POST /api/platform-admin/ai-config/summary-templates/propose/.
ROLE_PLATFORM_ADMIN only. Stateless: nothing is created by calling this,
save happens through the ordinary create endpoint afterward."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

PROPOSE_URL = "/api/platform-admin/ai-config/summary-templates/propose/"


def _valid_ai_response():
    return json.dumps(
        {
            "sections": [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}],
            "extra_instructions": "Always mention medication adherence.",
        },
    )


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="propose-root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
        is_email_verified=True,
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "propose-root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def test_a_valid_description_returns_sections_extra_instructions_and_a_preview(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), "This is a sample preview."],
    ):
        response = client.post(
            PROPOSE_URL,
            data=json.dumps({"description": "put everything in one section"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sections"] == [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]
    assert body["extra_instructions"] == "Always mention medication adherence."
    assert body["preview_text"] == "This is a sample preview."


def test_nothing_is_saved_by_calling_propose(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), "preview"],
    ):
        client.post(
            PROPOSE_URL,
            data=json.dumps({"description": "anything"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert not AISummaryTemplate.objects.exists()


def test_a_blank_description_is_rejected(client, platform_admin_auth):
    response = client.post(
        PROPOSE_URL,
        data=json.dumps({"description": ""}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400


def test_returns_503_when_the_ai_never_produces_a_valid_candidate(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = client.post(
            PROPOSE_URL,
            data=json.dumps({"description": "anything"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 503


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Propose Platform Endpoint Refusal Hospital")

    response = client.post(
        PROPOSE_URL,
        data=json.dumps({"description": "anything"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 403
