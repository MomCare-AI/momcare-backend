"""Platform-tier summary templates -- list/create/activate/deactivate under
/api/platform-admin/ai-config/summary-templates/. ROLE_PLATFORM_ADMIN only.
A template is plain text the admin writes; the AI judges whether it covers all 17
fields (mocked here -- see test_ai_summary_template_enhance.py and
core/ai/tests for the AI-facing behavior)."""

import json
from unittest.mock import patch

import pytest
from django.conf import settings

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "/api/platform-admin/ai-config/summary-templates/"


def _full_content():
    return "Start with the latest reading like 120/80, then the risk level, then the patient name and the rest."


@pytest.fixture(autouse=True)
def no_seeded_template():
    """Migration 0014 seeds a real active default template into every database,
    including the test one. These tests count and list templates, so start
    each from an empty table (rolled back with the test's transaction)."""
    AISummaryTemplate.objects.all().delete()


@pytest.fixture(autouse=True)
def covers_everything():
    """Default: the AI judges the template complete. Tests about missing or
    unverifiable coverage override this."""
    with patch("momcare_platform.core.platform_admin.api.views.check_template_coverage", return_value=[]) as m:
        yield m


@pytest.fixture
def platform_admin_auth(client):
    User.objects.create_user(
        email="templates-root@momcare.test",
        password="TestPass!2026",
        first_name="Root",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
        is_email_verified=True,
    )
    response = client.post(
        "/api/auth/login/",
        data={"email": "templates-root@momcare.test", "password": "TestPass!2026"},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def test_platform_admin_can_create_a_template(client, platform_admin_auth):
    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Baseline", "content": _full_content()}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    template = AISummaryTemplate.objects.get()
    assert template.organization is None
    assert template.content == _full_content()
    assert template.is_active is True


def test_organization_in_the_request_body_is_ignored(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Template Spoof Hospital")

    response = client.post(
        TEMPLATES_URL,
        data=json.dumps(
            {"name": "Sneaky", "content": _full_content(), "organization": str(hospital.org.id)},
        ),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    assert AISummaryTemplate.objects.get().organization is None


def test_list_only_returns_platform_tier_templates(client, platform_admin_auth, make_hospital):
    hospital = make_hospital("Template List Isolation Hospital")
    AISummaryTemplate.objects.create(organization=None, name="Platform", content=_full_content())
    AISummaryTemplate.objects.create(organization=hospital.org, name="Org", content=_full_content())

    response = client.get(TEMPLATES_URL, **platform_admin_auth)

    assert response.status_code == 200
    names = [row["name"] for row in response.json()["results"]]
    assert names == ["Platform"]


def test_activate_deactivates_the_previous_active_platform_template(client, platform_admin_auth):
    old = AISummaryTemplate.objects.create(
        organization=None,
        name="Old",
        content=_full_content(),
        is_active=True,
    )
    new = AISummaryTemplate.objects.create(organization=None, name="New", content=_full_content())

    response = client.post(f"{TEMPLATES_URL}{new.id}/activate/", **platform_admin_auth)

    assert response.status_code == 200
    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True


def test_activating_an_already_active_template_is_a_400(client, platform_admin_auth):
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        content=_full_content(),
        is_active=True,
    )

    response = client.post(f"{TEMPLATES_URL}{template.id}/activate/", **platform_admin_auth)

    assert response.status_code == 400


def test_the_active_template_cannot_be_deactivated(client, platform_admin_auth):
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        content=_full_content(),
        is_active=True,
    )

    response = client.post(f"{TEMPLATES_URL}{template.id}/deactivate/", **platform_admin_auth)

    assert response.status_code == 400
    assert "can't be deactivated" in response.json()["detail"]
    template.refresh_from_db()
    assert template.is_active is True


def test_deactivating_an_inactive_template_is_a_400(client, platform_admin_auth):
    template = AISummaryTemplate.objects.create(organization=None, name="Inactive", content=_full_content())

    response = client.post(f"{TEMPLATES_URL}{template.id}/deactivate/", **platform_admin_auth)

    assert response.status_code == 400


def test_saving_a_new_template_activates_it_and_deactivates_the_previous_one(client, platform_admin_auth):
    old = AISummaryTemplate.objects.create(
        organization=None,
        name="Old",
        content=_full_content(),
        is_active=True,
    )

    response = _post_content(client, platform_admin_auth, "A brand new template, reading first.")

    assert response.status_code == 201
    assert response.json()["is_active"] is True
    old.refresh_from_db()
    assert old.is_active is False
    assert AISummaryTemplate.objects.filter(organization__isnull=True, is_active=True).count() == 1
    assert AISummaryTemplate.objects.get(is_active=True).content == "A brand new template, reading first."


def test_a_rejected_template_leaves_the_previous_one_active(client, platform_admin_auth, covers_everything):
    old = AISummaryTemplate.objects.create(
        organization=None,
        name="Old",
        content=_full_content(),
        is_active=True,
    )
    covers_everything.return_value = ["nurse_name"]

    response = _post_content(client, platform_admin_auth, "Incomplete template.")

    assert response.status_code == 400
    old.refresh_from_db()
    assert old.is_active is True


def test_activating_an_organizations_template_via_the_platform_endpoint_is_404(
    client, platform_admin_auth, make_hospital
):
    hospital = make_hospital("Template Cross Tier Hospital")
    template = AISummaryTemplate.objects.create(organization=hospital.org, name="Org", content=_full_content())

    response = client.post(f"{TEMPLATES_URL}{template.id}/activate/", **platform_admin_auth)

    assert response.status_code == 404


def test_a_hospital_admin_is_refused(client, make_hospital, auth):
    hospital = make_hospital("Template Platform Endpoint Refusal Hospital")

    response = client.get(TEMPLATES_URL, **auth(hospital.admin.email))

    assert response.status_code == 403


def test_a_blank_name_is_rejected(client, platform_admin_auth):
    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "", "content": _full_content()}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 400
    assert not AISummaryTemplate.objects.exists()


def _post_content(client, auth, content):
    return client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "Baseline", "content": content}),
        content_type="application/json",
        **auth,
    )


def test_content_missing_fields_is_rejected_with_what_to_add(client, platform_admin_auth, covers_everything):
    covers_everything.return_value = ["nurse_name", "recent_note"]

    response = _post_content(client, platform_admin_auth, _full_content())

    assert response.status_code == 400
    body = response.json()
    assert body["missing_fields"] == ["nurse_name", "recent_note"]
    assert body["missing_labels"] == ["the nurse's name", "the most recent clinical note"]
    assert "the nurse's name" in body["content"][0]
    assert not AISummaryTemplate.objects.exists()


def test_503_and_nothing_saved_when_coverage_cannot_be_verified(client, platform_admin_auth, covers_everything):
    covers_everything.return_value = None

    response = _post_content(client, platform_admin_auth, _full_content())

    assert response.status_code == 503
    assert not AISummaryTemplate.objects.exists()


def test_content_over_the_word_limit_is_rejected_without_calling_the_ai(
    client, platform_admin_auth, covers_everything
):
    too_long = " ".join(f"w{i}" for i in range(131))

    response = _post_content(client, platform_admin_auth, too_long)

    assert response.status_code == 400
    assert "limit is 130" in json.dumps(response.json())
    covers_everything.assert_not_called()
    assert not AISummaryTemplate.objects.exists()


def test_content_at_exactly_the_word_limit_is_saved(client, platform_admin_auth):
    exactly = " ".join(f"w{i}" for i in range(130))

    response = _post_content(client, platform_admin_auth, exactly)

    assert response.status_code == 201
    assert response.json()["word_count"] == 130


def test_the_admin_writes_in_plain_words_no_special_syntax(client, platform_admin_auth):
    content = "Reading 120/80 first. Then risk level. Patient name last."

    response = _post_content(client, platform_admin_auth, content)

    assert response.status_code == 201
    assert AISummaryTemplate.objects.get().content == content


def test_blank_content_is_rejected(client, platform_admin_auth):
    assert _post_content(client, platform_admin_auth, "   ").status_code == 400


def test_extra_instructions_is_no_longer_part_of_a_template(client, platform_admin_auth):
    response = client.post(
        TEMPLATES_URL,
        data=json.dumps({"name": "No Wording", "content": _full_content(), "extra_instructions": "ignored"}),
        content_type="application/json",
        **platform_admin_auth,
    )

    assert response.status_code == 201
    assert "extra_instructions" not in response.json()
