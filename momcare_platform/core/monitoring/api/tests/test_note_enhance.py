"""POST /api/monitoring-notes/enhance/ -- AI-assist while drafting a
note. Stateless, doesn't touch any patient/note record. IsHospitalStaff
gated, same as who can already write notes."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.monitoring.models import MonitoringNote

pytestmark = pytest.mark.django_db

ENHANCE_URL = "/api/monitoring-notes/enhance/"


def test_hospital_staff_can_enhance_a_draft(client, make_hospital, auth):
    hospital = make_hospital("Note Enhance Hospital")

    with patch(
        "momcare_platform.core.ai.openrouter_client.generate",
        return_value="Patient reports mild ankle swelling, otherwise stable.",
    ):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"text": "pt c/o mild ankle swelling, otherwise ok"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert response.status_code == 200
    assert response.json()["enhanced_text"] == "Patient reports mild ankle swelling, otherwise stable."


def test_nothing_is_saved_by_calling_enhance(client, make_hospital, auth):
    hospital = make_hospital("Note Enhance No Save Hospital")

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enhanced."):
        client.post(
            ENHANCE_URL,
            data=json.dumps({"text": "draft note"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert not MonitoringNote.objects.exists()


def test_a_blank_text_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Note Enhance Blank Hospital")

    response = client.post(
        ENHANCE_URL,
        data=json.dumps({"text": ""}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_returns_503_when_the_enhance_call_fails(client, make_hospital, auth):
    hospital = make_hospital("Note Enhance Failure Hospital")

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value=None):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"text": "draft note"}),
            content_type="application/json",
            **auth(hospital.admin.email),
        )

    assert response.status_code == 503


def test_a_provider_can_also_enhance(client, make_hospital, make_staff, auth):
    """Any staff role that can write a note can also use the enhancer --
    same IsHospitalStaff gate as note creation itself."""
    hospital = make_hospital("Note Enhance Provider Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@noteenhance.test")

    with patch("momcare_platform.core.ai.openrouter_client.generate", return_value="Enhanced."):
        response = client.post(
            ENHANCE_URL,
            data=json.dumps({"text": "draft note"}),
            content_type="application/json",
            **auth(provider.email),
        )

    assert response.status_code == 200


def test_a_patient_role_is_refused(client, make_hospital, auth):
    from momcare_platform.core.users.models import Role, User

    User.objects.create_user(
        email="selfservice@noteenhance.test",
        password="TestPass!2026",
        first_name="Self",
        last_name="Service",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
        is_email_verified=True,
    )

    response = client.post(
        ENHANCE_URL,
        data=json.dumps({"text": "draft note"}),
        content_type="application/json",
        **auth("selfservice@noteenhance.test"),
    )

    assert response.status_code == 403
