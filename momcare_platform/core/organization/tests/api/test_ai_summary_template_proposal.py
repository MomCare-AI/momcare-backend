"""Organization-tier AI-assisted template authoring --
POST /api/organization/me/summary-templates/propose/. hospital_admin only,
same gate as the organization-tier summary templates it drafts for.
Stateless: nothing is created by calling this."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import TEMPLATE_FIELD_VOCABULARY

pytestmark = pytest.mark.django_db

PROPOSE_URL = "/api/organization/me/summary-templates/propose/"


def _valid_ai_response():
    return json.dumps(
        {
            "sections": [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}],
            "extra_instructions": "",
        },
    )


def test_a_valid_description_returns_sections_extra_instructions_and_a_preview(client, make_hospital, auth):
    hospital = make_hospital("Org Propose Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), "This is a sample preview."],
    ):
        response = client.post(
            PROPOSE_URL,
            data=json.dumps({"description": "put everything in one section"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sections"] == [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]
    assert body["preview_text"] == "This is a sample preview."


def test_nothing_is_saved_by_calling_propose(client, make_hospital, auth):
    hospital = make_hospital("Org Propose No Save Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        side_effect=[_valid_ai_response(), "preview"],
    ):
        client.post(
            PROPOSE_URL,
            data=json.dumps({"description": "anything"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert not AISummaryTemplate.objects.exists()


def test_a_blank_description_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Org Propose Blank Description Hospital")

    response = client.post(
        PROPOSE_URL,
        data=json.dumps({"description": ""}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_returns_503_when_the_ai_never_produces_a_valid_candidate(client, make_hospital, auth):
    hospital = make_hospital("Org Propose Failure Hospital")

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = client.post(
            PROPOSE_URL,
            data=json.dumps({"description": "anything"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert response.status_code == 503


def test_a_provider_is_refused(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Org Propose Provider Refusal Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@orgpropose.test")

    response = client.post(
        PROPOSE_URL,
        data=json.dumps({"description": "anything"}),
        content_type="application/json",
        **auth(provider.email),
    )

    assert response.status_code == 403
