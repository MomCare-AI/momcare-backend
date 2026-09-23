"""The hospital's own profile — a hospital_admin's PATCH surface on
``/api/organization/me/``.

Widened deliberately: name, contact, address, timezone, date_format and
established_date are all ordinary operational details a hospital should be
able to correct itself (a typo'd phone number, a corrected country) without
waiting on a platform admin to edit Django admin. What stays locked is only
the review state itself (``status``, ``reviewed_at``, ``review_note``) and
anything the model derives on its own (``region``, the counts) — never
something a hospital could self-report differently to get a different
answer.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.organization.models import Organization

pytestmark = pytest.mark.django_db

ORGANIZATION = "/api/organization/me/"


def patch(client, headers, **fields):
    return client.patch(
        ORGANIZATION,
        data=json.dumps(fields),
        content_type="application/json",
        **headers,
    )


def test_a_hospital_admin_can_update_ordinary_profile_fields(client, make_hospital, auth):
    hospital = make_hospital("Editable Hospital")

    response = patch(
        client,
        auth(hospital.admin.email),
        name="Editable Hospital — Renamed",
        phone="0512223333",
        email="new-contact@editable.test",
        address_line1="99 New Road",
        city="Lahore",
        state="Punjab",
        postal_code="54000",
        country="Pakistan",
        timezone="Asia/Karachi",
        date_format="DD-MM-YYYY",
        established_date="2010-01-01",
    )

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert hospital.org.name == "Editable Hospital — Renamed"
    assert hospital.org.phone == "0512223333"
    assert hospital.org.email == "new-contact@editable.test"
    assert hospital.org.address_line1 == "99 New Road"
    assert hospital.org.city == "Lahore"
    assert str(hospital.org.timezone) == "Asia/Karachi"
    assert hospital.org.date_format == "DD-MM-YYYY"
    assert str(hospital.org.established_date) == "2010-01-01"


def test_correcting_the_country_moves_the_region_with_it_over_the_api(client, make_hospital, auth):
    """The scenario CLAUDE.md documents as expected — a hospital corrects its
    country and the risk-model region recalculates — is now reachable
    through the hospital's own PATCH, not only via the ORM in a test."""
    hospital = make_hospital("Relocating Hospital")
    hospital.org.country = "Germany"
    hospital.org.save(update_fields=["country"])
    assert hospital.org.region is None

    response = patch(client, auth(hospital.admin.email), country="Nigeria")

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert hospital.org.country == "Nigeria"
    assert response.json()["region"] == "africa"


def test_a_non_admin_cannot_update_the_profile(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Locked Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@locked.test")
    original_name = hospital.org.name

    response = patch(client, auth(nurse.email), name="Should Not Apply")

    assert response.status_code == 403
    hospital.org.refresh_from_db()
    assert hospital.org.name == original_name


def test_review_state_cannot_be_changed_through_this_endpoint(client, make_hospital, auth):
    hospital = make_hospital("Review Locked Hospital")

    response = patch(
        client,
        auth(hospital.admin.email),
        status=Organization.STATUS_SUSPENDED,
        review_note="Trying to self-approve.",
    )

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert hospital.org.status == Organization.STATUS_APPROVED
    assert hospital.org.review_note == ""


def test_invalid_timezone_returns_400_not_500(client, make_hospital, auth):
    hospital = make_hospital("Bad Timezone Hospital")

    response = patch(client, auth(hospital.admin.email), timezone="Not/AZone")

    assert response.status_code == 400
    assert "timezone" in response.json()


def test_any_hospital_member_can_read_the_profile(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Readable Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@readable.test")

    response = client.get(ORGANIZATION, **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["name"] == "Readable Hospital"


def test_building_photo_is_no_longer_a_field(client, make_hospital, auth):
    """Regression guard: the field was removed outright, not just locked."""
    hospital = make_hospital("No Photo Hospital")

    response = client.get(ORGANIZATION, **auth(hospital.admin.email))

    assert "building_photo" not in response.json()


def test_a_hospital_admin_can_correct_the_license_number(client, make_hospital, auth):
    hospital = make_hospital("License Hospital")

    response = patch(client, auth(hospital.admin.email), license_number="LIC-9999")

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert hospital.org.license_number == "LIC-9999"


def test_license_image_is_read_only_here(client, make_hospital, auth):
    """The upload path is deliberately deferred -- this endpoint reports the
    field but does not accept writes to it yet."""
    hospital = make_hospital("License Image Hospital")

    response = patch(client, auth(hospital.admin.email), license_image="not-a-real-upload")

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert not hospital.org.license_image
