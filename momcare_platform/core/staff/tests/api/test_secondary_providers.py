"""External clinicians — the referral list.

Not platform staff: no login, no role, no permissions. The point of the model
is that ONE such clinician is shared across MANY patients, which is exactly
what ``Patient.emergency_contact_*`` is not.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.staff.models import SecondaryProvider

pytestmark = pytest.mark.django_db

PROVIDERS = "/api/secondary-providers/"


def post(client, headers, body, url=PROVIDERS):
    return client.post(url, data=json.dumps(body), content_type="application/json", **headers)


def patch(client, headers, url, body):
    return client.patch(url, data=json.dumps(body), content_type="application/json", **headers)


def make_provider(org, name="Dr. Ahmed Khan", **extra):
    return SecondaryProvider.objects.create(organization=org, name=name, **extra)


# ── Create ───────────────────────────────────────────────────────────────────


def test_an_admin_can_add_an_external_clinician(client, make_hospital, auth):
    hospital = make_hospital("Referral Hospital")

    response = post(
        client,
        auth(hospital.admin.email),
        {
            "name": "Dr. Ahmed Khan",
            "email": "ahmed@ruralclinic.test",
            "phone": "03005551234",
            "affiliation": "Rural Health Centre, Kahuta",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Dr. Ahmed Khan"
    assert body["affiliation"] == "Rural Health Centre, Kahuta"
    assert SecondaryProvider.objects.get(id=body["id"]).organization == hospital.org


def test_a_care_manager_can_add_one(client, make_hospital, make_staff, auth):
    hospital = make_hospital("CM Referral Hospital")
    cm = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@cmreferral.test")

    response = post(client, auth(cm.email), {"name": "Dr. Sara Malik"})

    assert response.status_code == 201


def test_a_nurse_cannot_add_one(client, make_hospital, make_staff, auth):
    """Curating the referral list is administrative work, not a clinical
    judgement made mid-shift."""
    hospital = make_hospital("Nurse Referral Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nursereferral.test")

    response = post(client, auth(nurse.email), {"name": "Dr. Nobody"})

    assert response.status_code == 403


def test_organization_is_never_taken_from_the_request_body(client, make_hospital, auth):
    """Even if a caller names another hospital, the record lands in their own."""
    hospital = make_hospital("Own Org Hospital")
    rival = make_hospital("Rival Org Hospital")

    response = post(
        client,
        auth(hospital.admin.email),
        {"name": "Dr. Injected", "organization": str(rival.org.id)},
    )

    assert response.status_code == 201
    assert SecondaryProvider.objects.get(id=response.json()["id"]).organization == hospital.org


def test_a_blank_name_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Blank Name Hospital")

    response = post(client, auth(hospital.admin.email), {"name": "   "})

    assert response.status_code == 400
    assert "name" in response.json()


# ── Read ─────────────────────────────────────────────────────────────────────


def test_the_list_shows_only_this_hospitals_clinicians(client, make_hospital, auth):
    hospital = make_hospital("Listing Hospital")
    rival = make_hospital("Rival Listing Hospital")
    make_provider(hospital.org, "Dr. Ours")
    make_provider(rival.org, "Dr. Theirs")

    body = client.get(PROVIDERS, **auth(hospital.admin.email)).json()

    assert [r["name"] for r in body["results"]] == ["Dr. Ours"]


def test_a_nurse_can_read_the_list(client, make_hospital, make_staff, auth):
    """Read is open to any hospital-side role — a nurse needs the referring
    doctor's number to make a call."""
    hospital = make_hospital("Nurse Read Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nurseread.test")
    make_provider(hospital.org)

    response = client.get(PROVIDERS, **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["count"] == 1


def test_search_matches_name_email_and_phone(client, make_hospital, auth):
    hospital = make_hospital("Search Referral Hospital")
    make_provider(hospital.org, "Dr. Findable", email="find@clinic.test", phone="03009998888")
    make_provider(hospital.org, "Dr. Other")
    headers = auth(hospital.admin.email)

    for term in ["Findable", "find@clinic.test", "03009998888"]:
        body = client.get(f"{PROVIDERS}?search={term}", **headers).json()
        assert [r["name"] for r in body["results"]] == ["Dr. Findable"], term


def test_another_hospitals_clinician_is_404_not_403(client, make_hospital, auth):
    hospital = make_hospital("Owner Referral Hospital")
    rival = make_hospital("Rival Detail Hospital")
    provider = make_provider(rival.org)

    response = client.get(f"{PROVIDERS}{provider.id}/", **auth(hospital.admin.email))

    assert response.status_code == 404


# ── The whole point: one clinician, many patients ────────────────────────────


def test_one_clinician_is_shared_across_many_patients(client, make_hospital, auth):
    """This is why it is a table and not columns on Patient: update the
    referring doctor's phone once, not once per patient."""
    hospital = make_hospital("Shared Referral Hospital")
    provider = make_provider(hospital.org, "Dr. Shared", phone="03001111111")
    for name in ["Ayesha", "Fatima", "Zara"]:
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": name, "last_name": "Bibi"},
        )
        patient.secondary_provider = provider
        patient.save(update_fields=["secondary_provider", "updated_at"])

    body = client.get(f"{PROVIDERS}{provider.id}/", **auth(hospital.admin.email)).json()

    assert body["patient_count"] == 3


def test_the_patient_record_shows_her_referring_clinician(client, make_hospital, auth):
    hospital = make_hospital("Patient Shows Referral Hospital")
    provider = make_provider(hospital.org, "Dr. Referrer", phone="03002223333")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )
    patient.secondary_provider = provider
    patient.save(update_fields=["secondary_provider", "updated_at"])

    body = client.get(f"/api/patients/{patient.id}/", **auth(hospital.admin.email)).json()

    assert body["secondary_provider_detail"]["name"] == "Dr. Referrer"
    assert body["secondary_provider_detail"]["phone"] == "03002223333"


# ── Update / delete ──────────────────────────────────────────────────────────


def test_an_admin_can_correct_the_details(client, make_hospital, auth):
    hospital = make_hospital("Edit Referral Hospital")
    provider = make_provider(hospital.org, phone="03001111111")

    response = patch(
        client,
        auth(hospital.admin.email),
        f"{PROVIDERS}{provider.id}/",
        {"phone": "03002222222"},
    )

    assert response.status_code == 200
    provider.refresh_from_db()
    assert provider.phone == "03002222222"


def test_a_nurse_cannot_edit(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse Edit Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nurseedit.test")
    provider = make_provider(hospital.org)

    response = patch(client, auth(nurse.email), f"{PROVIDERS}{provider.id}/", {"phone": "03009999999"})

    assert response.status_code == 403


def test_deleting_one_leaves_her_patient_record_intact(client, make_hospital, auth):
    """SET_NULL, not PROTECT or CASCADE — removing a name from the contact
    list must neither be blocked by, nor destroy, a clinical record."""
    hospital = make_hospital("Delete Referral Hospital")
    provider = make_provider(hospital.org)
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )
    patient.secondary_provider = provider
    patient.save(update_fields=["secondary_provider", "updated_at"])

    response = client.delete(f"{PROVIDERS}{provider.id}/", **auth(hospital.admin.email))

    assert response.status_code == 204
    patient.refresh_from_db()
    assert patient.id is not None
    assert patient.secondary_provider is None
    assert patient.full_name == "Ayesha Bibi"


def test_a_nurse_cannot_delete(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse Delete Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nursedelete.test")
    provider = make_provider(hospital.org)

    response = client.delete(f"{PROVIDERS}{provider.id}/", **auth(nurse.email))

    assert response.status_code == 403
    assert SecondaryProvider.objects.filter(id=provider.id).exists()


def test_another_hospital_cannot_delete_ours(client, make_hospital, auth):
    hospital = make_hospital("Owner Delete Hospital")
    rival = make_hospital("Rival Delete Hospital")
    provider = make_provider(hospital.org)

    response = client.delete(f"{PROVIDERS}{provider.id}/", **auth(rival.admin.email))

    assert response.status_code == 404
    assert SecondaryProvider.objects.filter(id=provider.id).exists()
