"""``/note-templates/`` -- the org/location-scoped canned-text catalogue."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import NoteTemplate

pytestmark = pytest.mark.django_db

TEMPLATES_URL = "/api/note-templates/"


def template_detail_url(template_id):
    return f"/api/note-templates/{template_id}/"


def post_json(client, url, data, **headers):
    return client.post(url, data=json.dumps(data), content_type="application/json", **headers)


# ── Visibility ────────────────────────────────────────────────────────────


def test_hospital_admin_sees_org_and_every_location_template(client, make_hospital, auth):
    hospital = make_hospital("Template Visibility Hospital")
    NoteTemplate.objects.create(title="Org Wide", content="x", organization=hospital.org)
    branch = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    NoteTemplate.objects.create(title="Branch Only", content="y", location=branch)

    response = client.get(TEMPLATES_URL, **auth(hospital.admin.email))

    titles = {t["title"] for t in response.json()["results"]}
    assert titles == {"Org Wide", "Branch Only"}


def test_location_scoped_staff_only_sees_org_templates_and_their_own_location(
    client,
    make_hospital,
    make_staff,
    auth,
):
    from momcare_platform.core.locations.services import ensure_default_location  # noqa: PLC0415

    hospital = make_hospital("Location Scoped Template Hospital")
    home_location = ensure_default_location(hospital.org)
    other_location = Location.objects.create(
        organization=hospital.org,
        name="Other Branch",
        location_manager=hospital.admin,
    )
    NoteTemplate.objects.create(title="Org Wide", content="x", organization=hospital.org)
    NoteTemplate.objects.create(title="Home Branch Template", content="y", location=home_location)
    NoteTemplate.objects.create(title="Other Branch Template", content="z", location=other_location)

    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@locscopetemplate.test")
    nurse.locations.set([home_location])

    response = client.get(TEMPLATES_URL, **auth(nurse.email))

    titles = {t["title"] for t in response.json()["results"]}
    assert titles == {"Org Wide", "Home Branch Template"}


def test_templates_never_cross_hospitals(client, make_hospital, auth):
    alpha = make_hospital("Alpha Template Hospital")
    beta = make_hospital("Beta Template Hospital")
    NoteTemplate.objects.create(title="Beta Secret", content="x", organization=beta.org)

    response = client.get(TEMPLATES_URL, **auth(alpha.admin.email))

    assert response.json()["count"] == 0


# ── Creating ──────────────────────────────────────────────────────────────


def test_hospital_admin_can_create_an_org_level_template(client, make_hospital, auth):
    hospital = make_hospital("Template Create Hospital")

    response = post_json(
        client,
        TEMPLATES_URL,
        {
            "title": "Routine Check-in",
            "content": "Patient reports feeling well.",
            "organization": str(hospital.org.id),
        },
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["title"] == "Routine Check-in"
    assert response.json()["created_by"] == str(hospital.admin.id)


def test_hospital_admin_can_create_a_location_level_template(client, make_hospital, auth):
    hospital = make_hospital("Template Create Location Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch X", location_manager=hospital.admin)

    response = post_json(
        client,
        TEMPLATES_URL,
        {"title": "Missed Appointment", "content": "No answer.", "location": str(branch.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["location"] == str(branch.id)


def test_providing_both_organization_and_location_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Both Fields Template Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch Y", location_manager=hospital.admin)

    response = post_json(
        client,
        TEMPLATES_URL,
        {"title": "Bad Template", "content": "x", "organization": str(hospital.org.id), "location": str(branch.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_a_nurse_cannot_create_a_template(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Create Template Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@notemplatecreate.test")

    response = post_json(
        client,
        TEMPLATES_URL,
        {"title": "Should Fail", "content": "x", "organization": str(hospital.org.id)},
        **auth(nurse.email),
    )

    assert response.status_code == 403


def test_a_provider_can_read_templates(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Provider Read Template Hospital")
    NoteTemplate.objects.create(title="Org Wide", content="x", organization=hospital.org)
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@readtemplate.test")

    response = client.get(TEMPLATES_URL, **auth(provider.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_cannot_create_a_template_naming_another_hospitals_organization(client, make_hospital, auth):
    alpha = make_hospital("Alpha Spoof Template Hospital")
    beta = make_hospital("Beta Spoof Template Hospital")

    response = post_json(
        client,
        TEMPLATES_URL,
        {"title": "Spoofed", "content": "x", "organization": str(beta.org.id)},
        **auth(alpha.admin.email),
    )

    assert response.status_code == 400


def test_duplicate_template_title_in_the_same_organization_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Duplicate Template API Hospital")
    NoteTemplate.objects.create(title="Routine Check-in", content="x", organization=hospital.org)

    response = post_json(
        client,
        TEMPLATES_URL,
        {"title": "Routine Check-in", "content": "y", "organization": str(hospital.org.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_blank_title_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Blank Title Hospital")

    response = post_json(
        client,
        TEMPLATES_URL,
        {"title": "   ", "content": "x", "organization": str(hospital.org.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


# ── Editing / deleting ────────────────────────────────────────────────────


def test_hospital_admin_can_edit_a_templates_content(client, make_hospital, auth):
    hospital = make_hospital("Template Edit Hospital")
    template = NoteTemplate.objects.create(title="Routine Check-in", content="Old text.", organization=hospital.org)

    response = client.patch(
        template_detail_url(template.id),
        data=json.dumps({"content": "New text."}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assert response.json()["content"] == "New text."
    assert response.json()["updated_by"] == str(hospital.admin.id)


def test_a_nurse_cannot_edit_a_template(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Edit Template Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@notemplateedit.test")
    template = NoteTemplate.objects.create(title="Protected", content="x", organization=hospital.org)

    response = client.patch(
        template_detail_url(template.id),
        data=json.dumps({"content": "Hacked."}),
        content_type="application/json",
        **auth(nurse.email),
    )

    assert response.status_code == 403


def test_a_nurse_cannot_delete_a_template(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Delete Template Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@notemplatedelete.test")
    template = NoteTemplate.objects.create(title="Protected", content="x", organization=hospital.org)

    response = client.delete(template_detail_url(template.id), **auth(nurse.email))

    assert response.status_code == 403
    assert NoteTemplate.objects.filter(id=template.id).exists()


def test_hospital_admin_can_delete_a_template(client, make_hospital, auth):
    hospital = make_hospital("Template Delete Hospital")
    template = NoteTemplate.objects.create(title="Removable", content="x", organization=hospital.org)

    response = client.delete(template_detail_url(template.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not NoteTemplate.objects.filter(id=template.id).exists()


def test_another_hospitals_template_resolves_to_404(client, make_hospital, auth):
    alpha = make_hospital("Alpha Template Detail Hospital")
    beta = make_hospital("Beta Template Detail Hospital")
    beta_template = NoteTemplate.objects.create(title="Beta Only", content="x", organization=beta.org)

    response = client.get(template_detail_url(beta_template.id), **auth(alpha.admin.email))

    assert response.status_code == 404
