"""The hospital's own building photo — the one field on Organization that's
actually writable from the portal. Everything else on this record is either
the evidence approval rested on, or drives the risk model's region, so it
stays locked; see MyOrganizationView's own docstring.
"""

import json

import pytest
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

pytestmark = pytest.mark.django_db

ORGANIZATION = "/api/organization/me/"


def _photo(name="building.jpg"):
    return SimpleUploadedFile(name, b"fake-image-bytes", content_type="image/jpeg")


def test_a_hospital_admin_can_upload_a_building_photo(client, make_hospital, auth):
    hospital = make_hospital("Photo Hospital")

    body = encode_multipart(BOUNDARY, {"building_photo": _photo()})
    response = client.patch(
        ORGANIZATION,
        data=body,
        content_type=MULTIPART_CONTENT,
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert hospital.org.building_photo.name


def test_a_non_admin_cannot_upload_a_building_photo(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Non Admin Photo Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nonadminphoto.test")

    body = encode_multipart(BOUNDARY, {"building_photo": _photo()})
    response = client.patch(
        ORGANIZATION,
        data=body,
        content_type=MULTIPART_CONTENT,
        **auth(nurse.email),
    )

    assert response.status_code == 403
    hospital.org.refresh_from_db()
    assert not hospital.org.building_photo


def test_a_hospital_admin_can_remove_the_building_photo(client, make_hospital, auth):
    hospital = make_hospital("Remove Photo Hospital")
    hospital.org.building_photo = _photo()
    hospital.org.save(update_fields=["building_photo", "updated_at"])

    response = client.patch(
        ORGANIZATION,
        data=json.dumps({"building_photo": None}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert not hospital.org.building_photo


def test_other_fields_cannot_be_changed_through_this_endpoint(
    client, make_hospital, auth,
):
    """The write serializer exposes only building_photo - proven by sending
    a field that would matter clinically (name) and confirming it's silently
    ignored rather than accepted, matching the existing region-is-read-only
    test's own pattern."""
    hospital = make_hospital("Locked Fields Hospital")
    original_name = hospital.org.name

    response = client.patch(
        ORGANIZATION,
        data=json.dumps({"name": "Renamed Without Review"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200, response.content
    hospital.org.refresh_from_db()
    assert hospital.org.name == original_name
