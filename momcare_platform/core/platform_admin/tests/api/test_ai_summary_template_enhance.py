"""Platform-tier template review step --
POST /api/platform-admin/ai-config/summary-templates/enhance/.
ROLE_PLATFORM_ADMIN only. Missing fields come back as an alert; a complete
template gets its wording polished and the entire summary previewed from sample data. Stateless:
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


DRAFT = "reading 120/80 first, then risk, then the patient name, and the rest"
ALL = json.dumps({"covered": list(TEMPLATE_FIELD_VOCABULARY)})


@pytest.fixture(autouse=True)
def no_seeded_template():
    """Migration 0014 seeds a real active default template into every database,
    including the test one. These tests count and list templates, so start
    each from an empty table (rolled back with the test's transaction)."""
    AISummaryTemplate.objects.all().delete()


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


def test_a_fully_covered_template_returns_enhanced_text_and_the_full_preview(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[ALL, "Polished draft.", ALL, "A sample preview."],
    ):
        response = _post(client, platform_admin_auth, {"content": DRAFT})

    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is True
    assert body["missing_fields"] == []
    assert body["enhanced_content"] == "Polished draft."
    assert body["preview_text"] == "A sample preview."
    assert body["word_limit"] == 130
    assert body["content"] == DRAFT


def test_an_incomplete_template_alerts_with_what_is_missing_in_plain_words(client, platform_admin_auth):
    covered = [f for f in TEMPLATE_FIELD_VOCABULARY if f != "recent_note"]

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value=json.dumps({"covered": covered}),
    ) as gen:
        response = _post(client, platform_admin_auth, {"content": DRAFT})

    assert gen.call_count == 1
    assert response.status_code == 200
    body = response.json()
    assert body["complete"] is False
    assert body["missing_fields"] == ["recent_note"]
    assert body["missing_labels"] == ["the most recent clinical note"]
    assert "the most recent clinical note" in body["message"]
    assert body["preview_text"] is None


def test_text_over_the_word_limit_is_a_400_and_calls_no_ai(client, platform_admin_auth):
    too_long = " ".join(f"w{i}" for i in range(131))

    with patch("momcare_platform.core.ai.openrouter_client.generate") as gen:
        response = _post(client, platform_admin_auth, {"content": too_long})

    gen.assert_not_called()
    assert response.status_code == 400
    assert "limit is 130" in json.dumps(response.json())


def test_blank_content_is_a_400(client, platform_admin_auth):
    assert _post(client, platform_admin_auth, {"content": "   "}).status_code == 400


def test_nothing_is_saved_by_calling_it(client, platform_admin_auth):
    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[ALL, "Polished.", ALL, "Preview."],
    ):
        _post(client, platform_admin_auth, {"content": DRAFT})

    assert not AISummaryTemplate.objects.exists()


def test_returns_503_when_the_ai_cannot_be_reached(client, platform_admin_auth):
    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = _post(client, platform_admin_auth, {"content": DRAFT})

    assert response.status_code == 503


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Enhance Platform Endpoint Refusal Hospital")

    response = _post(client, auth(hospital.admin.email), {"content": DRAFT})

    assert response.status_code == 403
