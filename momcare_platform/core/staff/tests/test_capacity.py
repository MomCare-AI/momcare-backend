"""Staff.current_patient_count counts across all three care-team roles a
staff member can hold (provider/nurse/care_manager), not just the lead.
"""

import datetime

import pytest
from django.conf import settings

from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def test_current_patient_count_includes_nurse_and_care_manager_roles(make_hospital, make_staff):
    hospital = make_hospital("Capacity Count Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@capacitycount.test")

    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha"},
        pregnancy_data={"lmp": datetime.date(2026, 1, 1), "nurse": nurse.staff},
    )

    assert nurse.staff.current_patient_count == 1


def test_current_patient_count_does_not_double_count_one_patient_in_two_roles(make_hospital, make_staff):
    """The same staff member in two slots on one pregnancy counts once."""
    hospital = make_hospital("No Double Count Hospital")
    staff_member = make_staff(hospital.org, settings.ROLE_PROVIDER, "dual@nodoublecount.test")

    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha"},
        pregnancy_data={
            "lmp": datetime.date(2026, 1, 1),
            "provider": staff_member.staff,
            "nurse": staff_member.staff,
        },
    )

    assert staff_member.staff.current_patient_count == 1


def test_a_delivered_pregnancy_no_longer_counts(make_hospital, make_staff):
    hospital = make_hospital("Delivered Capacity Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@deliveredcapacity.test")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha"},
        pregnancy_data={"lmp": datetime.date(2026, 1, 1), "nurse": nurse.staff},
    )
    pregnancy = patient.pregnancies.get()
    pregnancy.status = "delivered"
    pregnancy.save(update_fields=["status", "updated_at"])

    assert nurse.staff.current_patient_count == 0


def test_has_capacity_blocks_once_max_patients_is_reached(make_hospital, make_staff):
    hospital = make_hospital("Has Capacity Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@hascapacity.test")
    nurse.staff.max_patients = 1
    nurse.staff.save(update_fields=["max_patients"])
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha"},
        pregnancy_data={"lmp": datetime.date(2026, 1, 1), "nurse": nurse.staff},
    )

    assert nurse.staff.has_capacity is False
