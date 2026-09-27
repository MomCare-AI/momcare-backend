"""New ``GET /api/patients/`` filters -- ``location``/``is_active``/
``care_manager``/``provider``/``nurse`` -- added alongside the Care
Activities dashboard work, matching Neuro_RPM's own ``_scoped_queryset()``
filter set. The three staff filters reach through the active pregnancy,
since that's where care team actually lives (see CLAUDE.md's "why staff
attaches to Pregnancy, not Patient").
"""

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.locations.models import Location
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha", *, pregnancy_data=None, location=None):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data=pregnancy_data,
        )
        if location is not None:
            patient.location = location
            patient.save(update_fields=["location"])
        return patient

    return _make


def names_in(body):
    return [row["full_name"] for row in body["results"]]


def test_location_filter(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Location Filter Hospital")
    # Onboard the main-branch patient BEFORE creating the second location --
    # ensure_default_location() reuses any existing active location for the
    # org, so creating "Branch 2" first would put both patients on it.
    patient_for(hospital, "MainBranch")
    branch = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    patient_for(hospital, "SecondBranch", location=branch)

    response = client.get(f"{PATIENTS}?location={branch.id}", **auth(hospital.admin.email))

    assert names_in(response.json()) == ["SecondBranch Bibi"]


def test_is_active_true_filter(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Active Filter Hospital")
    active = patient_for(hospital, "StillActive")
    inactive = patient_for(hospital, "NoLongerActive")
    inactive.deactivate(by=hospital.admin, reason="Left")

    response = client.get(f"{PATIENTS}?is_active=true", **auth(hospital.admin.email))

    assert names_in(response.json()) == [active.full_name]


def test_is_active_false_filter(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Inactive Filter Hospital")
    patient_for(hospital, "StillActive")
    inactive = patient_for(hospital, "NoLongerActive")
    inactive.deactivate(by=hospital.admin, reason="Left")

    response = client.get(f"{PATIENTS}?is_active=false", **auth(hospital.admin.email))

    assert names_in(response.json()) == [inactive.full_name]


def test_provider_filter(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Provider Filter Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@providerfilter.test")
    other_provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "other@providerfilter.test")
    mine = patient_for(hospital, "Mine", pregnancy_data={"lmp": timezone.now().date(), "provider": provider.staff})
    patient_for(hospital, "NotMine", pregnancy_data={"lmp": timezone.now().date(), "provider": other_provider.staff})

    response = client.get(f"{PATIENTS}?provider={provider.staff.id}", **auth(hospital.admin.email))

    assert names_in(response.json()) == [mine.full_name]


def test_nurse_filter(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Nurse Filter Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nursefilter.test")
    mine = patient_for(hospital, "Mine", pregnancy_data={"lmp": timezone.now().date(), "nurse": nurse.staff})
    patient_for(hospital, "NotMine")

    response = client.get(f"{PATIENTS}?nurse={nurse.staff.id}", **auth(hospital.admin.email))

    assert names_in(response.json()) == [mine.full_name]


def test_care_manager_filter(client, make_hospital, make_staff, patient_for, auth):
    hospital = make_hospital("Care Manager Filter Hospital")
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@cmfilter.test")
    mine = patient_for(
        hospital,
        "Mine",
        pregnancy_data={"lmp": timezone.now().date(), "care_manager": care_manager.staff},
    )
    patient_for(hospital, "NotMine")

    response = client.get(f"{PATIENTS}?care_manager={care_manager.staff.id}", **auth(hospital.admin.email))

    assert names_in(response.json()) == [mine.full_name]


def test_filters_never_reach_across_hospitals(client, make_hospital, make_staff, patient_for, auth):
    """A location/staff id from another hospital must resolve to an empty
    list, not another tenant's patient."""
    hospital = make_hospital("Filter Isolation Hospital A")
    rival = make_hospital("Filter Isolation Hospital B")
    rival_provider = make_staff(rival.org, settings.ROLE_PROVIDER, "provider@filterisolation.test")
    patient_for(hospital)
    patient_for(rival, pregnancy_data={"lmp": timezone.now().date(), "provider": rival_provider.staff})

    response = client.get(f"{PATIENTS}?provider={rival_provider.staff.id}", **auth(hospital.admin.email))

    assert response.json()["results"] == []


def test_filters_can_combine_with_search(client, make_hospital, patient_for, auth):
    hospital = make_hospital("Combined Filter Hospital")
    patient_for(hospital, "FindMe")
    inactive = patient_for(hospital, "FindMeToo")
    inactive.deactivate(by=hospital.admin, reason="Left")

    response = client.get(f"{PATIENTS}?is_active=true&search=FindMe", **auth(hospital.admin.email))

    assert names_in(response.json()) == ["FindMe Bibi"]
