"""``/status-labels/`` -- the org/location-scoped status catalogue."""

import json

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import StatusLabel

pytestmark = pytest.mark.django_db

LABELS_URL = "/api/status-labels/"


def label_detail_url(label_id):
    return f"/api/status-labels/{label_id}/"


def post_json(client, url, data, **headers):
    return client.post(url, data=json.dumps(data), content_type="application/json", **headers)


# ── Visibility ────────────────────────────────────────────────────────────


def test_hospital_admin_sees_org_and_every_location_label(client, make_hospital, auth):
    hospital = make_hospital("Label Visibility Hospital")
    StatusLabel.objects.create(name="Org Wide", organization=hospital.org)
    branch = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    StatusLabel.objects.create(name="Branch Only", location=branch)

    response = client.get(LABELS_URL, **auth(hospital.admin.email))

    names = {label["name"] for label in response.json()["results"]}
    assert names == {"Org Wide", "Branch Only"}


def test_location_scoped_staff_only_sees_org_labels_and_their_own_location(client, make_hospital, make_staff, auth):
    from momcare_platform.core.locations.services import ensure_default_location  # noqa: PLC0415

    hospital = make_hospital("Location Scoped Label Hospital")
    home_location = ensure_default_location(hospital.org)
    other_location = Location.objects.create(
        organization=hospital.org,
        name="Other Branch",
        location_manager=hospital.admin,
    )
    StatusLabel.objects.create(name="Org Wide", organization=hospital.org)
    StatusLabel.objects.create(name="Home Branch Label", location=home_location)
    StatusLabel.objects.create(name="Other Branch Label", location=other_location)

    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@locscopelabel.test")
    nurse.locations.set([home_location])

    response = client.get(LABELS_URL, **auth(nurse.email))

    names = {label["name"] for label in response.json()["results"]}
    assert names == {"Org Wide", "Home Branch Label"}


def test_labels_never_cross_hospitals(client, make_hospital, auth):
    alpha = make_hospital("Alpha Label Hospital")
    beta = make_hospital("Beta Label Hospital")
    StatusLabel.objects.create(name="Beta Secret", organization=beta.org)

    response = client.get(LABELS_URL, **auth(alpha.admin.email))

    assert response.json()["count"] == 0


# ── Creating ──────────────────────────────────────────────────────────────


def test_hospital_admin_can_create_an_org_level_label(client, make_hospital, auth):
    hospital = make_hospital("Label Create Hospital")

    response = post_json(
        client,
        LABELS_URL,
        {"name": "Critical", "description": "Needs urgent review", "organization": str(hospital.org.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Critical"


def test_hospital_admin_can_create_a_location_level_label(client, make_hospital, auth):
    hospital = make_hospital("Label Create Location Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch X", location_manager=hospital.admin)

    response = post_json(
        client,
        LABELS_URL,
        {"name": "Waiting", "location": str(branch.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 201
    assert response.json()["location"] == str(branch.id)


def test_providing_both_organization_and_location_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Both Fields Label Hospital")
    branch = Location.objects.create(organization=hospital.org, name="Branch Y", location_manager=hospital.admin)

    response = post_json(
        client,
        LABELS_URL,
        {"name": "Bad Label", "organization": str(hospital.org.id), "location": str(branch.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


def test_a_nurse_cannot_create_a_label(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Create Label Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@nolabelcreate.test")

    response = post_json(
        client, LABELS_URL, {"name": "Should Fail", "organization": str(hospital.org.id)}, **auth(nurse.email)
    )

    assert response.status_code == 403


def test_a_provider_cannot_create_a_label(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Provider No Create Label Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@nolabelcreate.test")

    response = post_json(
        client, LABELS_URL, {"name": "Should Fail", "organization": str(hospital.org.id)}, **auth(provider.email)
    )

    assert response.status_code == 403


def test_a_provider_can_read_labels(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Provider Read Label Hospital")
    StatusLabel.objects.create(name="Org Wide", organization=hospital.org)
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, email="provider@readlabel.test")

    response = client.get(LABELS_URL, **auth(provider.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_cannot_create_a_label_naming_another_hospitals_organization(client, make_hospital, auth):
    alpha = make_hospital("Alpha Spoof Label Hospital")
    beta = make_hospital("Beta Spoof Label Hospital")

    response = post_json(
        client,
        LABELS_URL,
        {"name": "Spoofed", "organization": str(beta.org.id)},
        **auth(alpha.admin.email),
    )

    assert response.status_code == 400


def test_duplicate_label_name_in_the_same_organization_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Duplicate Label Hospital")
    StatusLabel.objects.create(name="High Risk", organization=hospital.org)

    response = post_json(
        client,
        LABELS_URL,
        {"name": "High Risk", "organization": str(hospital.org.id)},
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400


# ── Editing / deleting ────────────────────────────────────────────────────


def test_hospital_admin_can_rename_and_recolor_a_label(client, make_hospital, auth):
    hospital = make_hospital("Label Rename Hospital")
    label = StatusLabel.objects.create(name="Old Name", organization=hospital.org)

    response = client.patch(
        label_detail_url(label.id),
        data=json.dumps({"name": "New Name", "color": "#123456"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200
    assert response.json()["name"] == "New Name"
    assert response.json()["color"] == "#123456"


def test_a_nurse_cannot_delete_a_label(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse No Delete Label Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, email="nurse@nolabeldelete.test")
    label = StatusLabel.objects.create(name="Protected Label", organization=hospital.org)

    response = client.delete(label_detail_url(label.id), **auth(nurse.email))

    assert response.status_code == 403
    assert StatusLabel.objects.filter(id=label.id).exists()


def test_hospital_admin_can_delete_a_label(client, make_hospital, auth):
    hospital = make_hospital("Label Delete Hospital")
    label = StatusLabel.objects.create(name="Removable Label", organization=hospital.org)

    response = client.delete(label_detail_url(label.id), **auth(hospital.admin.email))

    assert response.status_code == 204
    assert not StatusLabel.objects.filter(id=label.id).exists()


def test_another_hospitals_label_resolves_to_404(client, make_hospital, auth):
    alpha = make_hospital("Alpha Label Detail Hospital")
    beta = make_hospital("Beta Label Detail Hospital")
    beta_label = StatusLabel.objects.create(name="Beta Only", organization=beta.org)

    response = client.get(label_detail_url(beta_label.id), **auth(alpha.admin.email))

    assert response.status_code == 404
