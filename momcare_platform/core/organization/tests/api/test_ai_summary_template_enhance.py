"""Organization-tier AI-assisted template wording --
POST /api/organization/me/summary-templates/enhance/. hospital_admin
only, same gate as the organization-tier summary templates it drafts
for. Stateless: nothing is created by calling this."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY

pytestmark = pytest.mark.django_db

ENHANCE_URL = "/api/organization/me/summary-templates/enhance/"


def _full_sections(label="All Fields"):
    return [{"label": label, "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_a_valid_request_returns_enhanced_wording_word_count_and_preview(client, make_hospital, auth):
    hospital = make_hospital("Org Enhance Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Always mention medication adherence clearly.", "A sample preview."],
    ):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections(), "extra_instructions": "mention adherence"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["extra_instructions"] == "Always mention medication adherence clearly."
    assert body["word_count"] == 5
    assert body["preview_text"] == "A sample preview."


def test_nothing_is_saved_by_calling_enhance(client, make_hospital, auth):
    hospital = make_hospital("Org Enhance No Save Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=["Polished.", "Preview."],
    ):
        client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections(), "extra_instructions": "draft"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert not AISummaryTemplate.objects.exists()


def test_invalid_sections_are_rejected(client, make_hospital, auth):
    hospital = make_hospital("Org Enhance Invalid Sections Hospital")
    incomplete = [{"label": "Incomplete", "fields": ["patient_name"]}]

    response = client.post(
        ENHANCE_URL,
        data=json.dumps({"sections": incomplete, "extra_instructions": "draft"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_returns_503_when_the_enhance_call_fails(client, make_hospital, auth):
    hospital = make_hospital("Org Enhance Failure Hospital")

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"sections": _full_sections(), "extra_instructions": "draft"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert response.status_code == 503


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Org Enhance Provider Refusal Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@orgenhance.test")

    response = client.post(
        ENHANCE_URL,
        data=json.dumps({"sections": _full_sections(), "extra_instructions": "draft"}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403
