"""Platform-tier AI-assisted template wording --
POST /api/platform-admin/ai-config/summary-templates/enhance/.
ROLE_PLATFORM_ADMIN only. The admin builds sections by hand and sends
them plus their current wording draft; this only ever polishes wording,
never structure. Stateless: nothing is created by calling this."""

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


def test_a_valid_request_returns_enhanced_wording_word_count_and_preview(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Always mention medication adherence clearly.", "A sample preview."],
    ):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections(), "extra_instructions": "mention adherence"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["extra_instructions"] == "Always mention medication adherence clearly."
    assert body["word_count"] == 5
    assert body["word_limit"] == 150
    assert body["preview_text"] == "A sample preview."
    assert body["sections"] == _full_sections()


def test_sections_are_echoed_back_unchanged(client, platform_admin_auth):
    sections = _full_sections("My Layout")
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Polished.", "Preview."],
    ):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": sections, "extra_instructions": "draft"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.json()["sections"] == sections


def test_nothing_is_saved_by_calling_enhance(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Polished.", "Preview."],
    ):
        client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections(), "extra_instructions": "draft"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert not AISummaryTemplate.objects.exists()


def test_invalid_sections_are_rejected(client, platform_admin_auth):
    incomplete = [{"label": "Incomplete", "fields": ["patient_name"]}]

    response = client.post(
        ENHANCE_URL,
        data=json.dumps({"sections": incomplete, "extra_instructions": "draft"}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400


def test_extra_instructions_defaults_to_blank_when_omitted(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Suggested starting wording.", "Preview."],
    ) as mock_generate:
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections()}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 200
    assert mock_generate.called


def test_returns_503_when_the_enhance_call_fails(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections(), "extra_instructions": "draft"}),
            content_type="application/json",
            **platform_admin_auth,
        )

    assert response.status_code == 503


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Enhance Platform Endpoint Refusal Hospital")

    response = client.post(
        ENHANCE_URL,
        data=json.dumps({"sections": _full_sections(), "extra_instructions": "draft"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 403
