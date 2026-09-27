"""``GET /api/patients/quick-lookup-kpis/`` -- organization-wide Staff/Patient
totals across every location, matching Neuro_RPM's own `quick-lookup-kpis`.

Deliberately never scoped by `?location=`/`?assigned_to=me` (unlike
`dashboard-kpis`) -- but, unlike Neuro_RPM's own single-tenant version,
always scoped to the caller's own organization. See
`PatientQuickLookupKpisView`'s own docstring for the full reasoning.
"""

import pytest
from django.conf import settings

from momcare_platform.core.locations.models import Location
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

KPIS = "/api/patients/quick-lookup-kpis/"


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha", *, location=None):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
        )
        if location is not None:
            patient.location = location
            patient.save(update_fields=["location"])
        return patient

    return _make


def test_shape_with_nothing_in_the_hospital(client, make_hospital, auth):
    hospital = make_hospital("Empty Quick Lookup Hospital")

    response = client.get(KPIS, **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json() == {
        "staff": {"total": 0, "active": 0, "inactive": 0},
        "patients": {"total": 0, "active": 0, "inactive": 0},
    }


def test_patient_counts_split_active_and_inactive(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Patient Split Hospital")
    patient_for(hospital, "StillActive")
    inactive = patient_for(hospital, "NoLongerActive")
    inactive.deactivate(by=hospital.admin, reason="Left")

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["patients"] == {"total": 2, "active": 1, "inactive": 1}


def test_staff_counts_split_active_and_inactive(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Staff Split Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@staffsplit.test")
    nurse.staff.deactivate(by=hospital.admin, reason="Left")
    make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@staffsplit.test")

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body["staff"] == {"total": 2, "active": 1, "inactive": 1}


def test_counts_are_never_scoped_by_location(client, make_hospital, patient_for, auth):
    """dashboard-kpis respects ?location=; this endpoint never does, by
    design -- it's the whole-hospital total regardless of location filter."""
    hospital = make_hospital("Multi Location Hospital")
    patient_for(hospital, "MainBranch")
    branch = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    patient_for(hospital, "SecondBranch", location=branch)

    # Passing ?location= must have no effect at all -- the view doesn't even
    # read it.
    body = client.get(f"{KPIS}?location={branch.id}", **auth(hospital.admin.email)).json()

    assert body["patients"]["total"] == 2


def test_counts_are_never_scoped_by_assigned_to_me(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Assigned To Me Ignored Hospital")
    make_staff(hospital.org, settings.ROLE_NURSE, "nurse@assignedignored.test")
    patient_for(hospital)

    body = client.get(f"{KPIS}?assigned_to=me", **auth("nurse@assignedignored.test")).json()

    assert body["patients"]["total"] == 1


def test_counts_are_not_readable_across_hospitals(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Quick Lookup Isolation Hospital A")
    rival = make_hospital("Quick Lookup Isolation Hospital B")
    patient_for(rival)
    patient_for(rival)
    make_staff(rival.org, settings.ROLE_NURSE, "nurse@quicklookupisolation.test")

    body = client.get(KPIS, **auth(hospital.admin.email)).json()

    assert body == {
        "staff": {"total": 0, "active": 0, "inactive": 0},
        "patients": {"total": 0, "active": 0, "inactive": 0},
    }


def test_a_non_staff_role_is_rejected(client, make_hospital, auth):
    """Fault injection: same IsHospitalStaff gate as the rest of this app."""
    from momcare_platform.core.users.models import Role, User

    hospital = make_hospital("Quick Lookup Patient Rejected Hospital")
    patient_user = User.objects.create_user(
        email="patient@quicklookuprejected.test",
        password="TestPass!2026",
        first_name="Some",
        last_name="Patient",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    patient_user.organization = hospital.org
    patient_user.is_email_verified = True
    patient_user.save(update_fields=["organization", "is_email_verified", "updated_at"])

    response = client.get(KPIS, **auth("patient@quicklookuprejected.test"))

    assert response.status_code == 403
