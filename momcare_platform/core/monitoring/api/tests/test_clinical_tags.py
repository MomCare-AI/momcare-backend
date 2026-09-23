"""``/clinical-tags/`` -- the org/location-scoped tag catalogue."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import ClinicalTag

pytestmark = pytest.mark.django_db

TAGS_URL = "/api/clinical-tags/"


def tag_detail_url(tag_id):
    return f"/api/clinical-tags/{tag_id}/"


def post_json(client, url, data, **headers):
    return client.post(url, data=json.dumps(data), content_type="application/json", **headers)


# ── Visibility ────────────────────────────────────────────────────────────


def test_hospital_admin_sees_org_and_every_location_tag(client, make_hospital, auth):
    hospital = make_hospital("Tag Visibility Hospital")
    ClinicalTag.objects.create(name="Org Wide", organization=hospital.org)
    branch = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    ClinicalTag.objects.create(name="Branch Only", location=branch)

    response = client.get(TAGS_URL, **auth(hospital.admin.email))

    names = {t["name"] for t in response.json()["results"]}
    assert names == {"Org Wide", "Branch Only"}


def test_location_scoped_staff_only_sees_org_tags_and_their_own_location(client, make_hospital, make_staff, auth):
    from momcare_platform.core.locations.services import ensure_default_location  # noqa: PLC0415

    hospital = make_hospital("Location Scoped Tag Hospital")
    home_location = ensure_default_location(hospital.org)
    other_location = Location.objects.create(
        organization=hospital.org,
        name="Other Branch",
        location_manager=hospital.admin,
    )
    ClinicalTag.objects.create(name="Org Wide", organization=hospital.org)
    ClinicalTag.objects.create(name="Home Branch Tag", location=home_location)
    ClinicalTag.objects.create(name="Other Branch Tag", location=other_location)

    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@locscopetag.test")
    nurse.locations.set([home_location])

    response = client.get(TAGS_URL, **auth(nurse.email))

    names = {t["name"] for t in response.json()["results"]}
    assert names == {"Org Wide", "Home Branch Tag"}


def test_tags_never_cross_hospitals(client, make_hospital, auth):
    alpha = make_hospital("Alpha Tag Hospital")
    beta = make_hospital("Beta Tag Hospital")
    ClinicalTag.objects.create(name="Beta Secret", organization=beta.org)

    response = client.get(TAGS_URL, **auth(alpha.admin.email))

    assert response.json()["count"] == 0


# ── Creating ──────────────────────────────────────────────────────────────


def test_hospital_admin_can_create_an_org_level_tag(client, make_hospital, auth):
    hospital = make_hospital("Tag Create Hospital")

    response = post_json(
        client,
        TAGS_URL,
        {"name": "New Org Tag", "organization": str(hospital.org.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["name"] == "New Org Tag"


def test_hospital_admin_can_create_a_location_level_tag(client, make_hospital, auth):
    hospital = make_hospital("Tag Create Location Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch X", location_manager=hospital.admin)

    response = post_json(
        client,
        TAGS_URL,
        {"name": "New Branch Tag", "location": str(branch.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["location"] == str(branch.id)


def test_providing_both_organization_and_location_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Both Fields Tag Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch Y", location_manager=hospital.admin)

    response = post_json(
        client,
        TAGS_URL,
        {"name": "Bad Tag", "organization": str(hospital.org.id), "location": str(branch.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_a_nurse_cannot_create_a_tag(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Create Tag Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@notagcreate.test")

    response = post_json(
        client, TAGS_URL, {"name": "Should Fail", "organization": str(hospital.org.id)}, **auth(nurse.email)
    )

    assert response.status_code == 403


def test_cannot_create_a_tag_naming_another_hospitals_organization(client, make_hospital, auth):
    alpha = make_hospital("Alpha Spoof Tag Hospital")
    beta = make_hospital("Beta Spoof Tag Hospital")

    response = post_json(
        client,
        TAGS_URL,
        {"name": "Spoofed", "organization": str(beta.org.id)},
        **auth(alpha.admin.email),
    )

    assert response.status_code == 400


def test_cannot_create_a_tag_under_another_hospitals_location(client, make_hospital, auth):
    alpha = make_hospital("Alpha Location Spoof Hospital")
    beta = make_hospital("Beta Location Spoof Hospital")
    beta_location = Location.objects.create(organization=beta.org, name="Beta Branch", location_manager=beta.admin)

    response = post_json(
        client,
        TAGS_URL,
        {"name": "Spoofed Location Tag", "location": str(beta_location.id)},
        **auth(alpha.admin.email),
    )

    assert response.status_code == 400


# ── Editing / deleting ────────────────────────────────────────────────────


def test_hospital_admin_can_rename_and_recolor_a_tag(client, make_hospital, auth):
    hospital = make_hospital("Tag Rename Hospital")
    tag = ClinicalTag.objects.create(name="Old Name", organization=hospital.org)

    response = client.patch(
        tag_detail_url(tag.id),
        data=json.dumps({"name": "New Name", "color": "#123456"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"
    assert response.json()["color"] == "#123456"


def test_a_nurse_cannot_delete_a_tag(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Delete Tag Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@notagdelete.test")
    tag = ClinicalTag.objects.create(name="Protected Tag", organization=hospital.org)

    response = client.delete(tag_detail_url(tag.id), **auth(nurse.email))

    assert response.status_code == 403
    assert ClinicalTag.objects.filter(id=tag.id).exists()


def test_hospital_admin_can_delete_a_tag(client, make_hospital, auth):
    hospital = make_hospital("Tag Delete Hospital")
    tag = ClinicalTag.objects.create(name="Removable Tag", organization=hospital.org)

    response = client.delete(tag_detail_url(tag.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not ClinicalTag.objects.filter(id=tag.id).exists()


def test_another_hospitals_tag_resolves_to_404(client, make_hospital, auth):
    alpha = make_hospital("Alpha Tag Detail Hospital")
    beta = make_hospital("Beta Tag Detail Hospital")
    beta_tag = ClinicalTag.objects.create(name="Beta Only", organization=beta.org)

    response = client.get(tag_detail_url(beta_tag.id), **auth(alpha.admin.email))

    assert response.status_code == 404


def test_duplicate_tag_name_in_the_same_organization_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Duplicate Tag Hospital")
    ClinicalTag.objects.create(name="High Risk", organization=hospital.org)

    response = post_json(
        client,
        TAGS_URL,
        {"name": "High Risk", "organization": str(hospital.org.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400
