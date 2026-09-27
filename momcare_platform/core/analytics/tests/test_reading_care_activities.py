"""``patients_with_unseen_readings``/``patients_needing_reading_reminder`` --
pure service-level tests, no HTTP. Both fields are decoupled via direct
state setup (not real ``VitalReading`` creation) to isolate the query logic
from the signal that populates the fields -- that signal has its own
coverage in ``modules/pregnancy/vitals/tests/test_signals.py``.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from momcare_platform.core.analytics.services import (
    READING_REMINDER_GAP_DAYS,
    UNSEEN_READINGS_WINDOW_DAYS,
    patients_needing_reading_reminder,
    patients_with_unseen_readings,
)
from momcare_platform.core.patients.models import Patient
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_with_pregnancy(make_hospital):
    def _make(hospital, first_name="Ayesha"):
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        )
        return patient

    return _make


def _set(patient, **fields):
    Patient.objects.filter(pk=patient.pk).update(**fields)
    patient.refresh_from_db()
    return patient


# ── Unseen Readings ───────────────────────────────────────────────────────


def test_a_reading_after_the_last_contact_qualifies(make_hospital, patient_with_pregnancy):
    hospital = make_hospital("Unseen Reading Hospital")
    patient = patient_with_pregnancy(hospital)
    now = timezone.now()
    _set(patient, last_monitoring_contact_at=now - timedelta(days=2), last_reading_at=now - timedelta(hours=1))

    qualifying = patients_with_unseen_readings(Patient.objects.filter(organization=hospital.org))

    assert patient in qualifying


def test_a_reading_before_the_last_contact_does_not_qualify(make_hospital, patient_with_pregnancy):
    """Staff already saw the state that produced this reading -- contact
    happened after it, not before."""
    hospital = make_hospital("Already Seen Hospital")
    patient = patient_with_pregnancy(hospital)
    now = timezone.now()
    _set(patient, last_reading_at=now - timedelta(days=2), last_monitoring_contact_at=now - timedelta(hours=1))

    qualifying = patients_with_unseen_readings(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_a_patient_with_no_reading_at_all_does_not_qualify(make_hospital, patient_with_pregnancy):
    hospital = make_hospital("No Reading Hospital")
    patient = patient_with_pregnancy(hospital)

    qualifying = patients_with_unseen_readings(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_a_reading_never_contacted_about_but_outside_the_window_does_not_qualify(
    make_hospital,
    patient_with_pregnancy,
):
    hospital = make_hospital("Stale Unseen Reading Hospital")
    patient = patient_with_pregnancy(hospital)
    stale = timezone.now() - timedelta(days=UNSEEN_READINGS_WINDOW_DAYS + 1)
    _set(patient, last_reading_at=stale, last_monitoring_contact_at=None)

    qualifying = patients_with_unseen_readings(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_never_contacted_but_recent_reading_qualifies(make_hospital, patient_with_pregnancy):
    hospital = make_hospital("Never Contacted Hospital")
    patient = patient_with_pregnancy(hospital)
    _set(patient, last_reading_at=timezone.now() - timedelta(hours=1), last_monitoring_contact_at=None)

    qualifying = patients_with_unseen_readings(Patient.objects.filter(organization=hospital.org))

    assert patient in qualifying


# ── Reading Reminder ──────────────────────────────────────────────────────


def test_no_reading_at_all_qualifies_immediately(make_hospital, patient_with_pregnancy):
    hospital = make_hospital("No Reading Reminder Hospital")
    patient = patient_with_pregnancy(hospital)

    qualifying = patients_needing_reading_reminder(Patient.objects.filter(organization=hospital.org))

    assert patient in qualifying


def test_a_recent_reading_does_not_qualify(make_hospital, patient_with_pregnancy):
    hospital = make_hospital("Recent Reading Hospital")
    patient = patient_with_pregnancy(hospital)
    _set(patient, last_reading_at=timezone.now() - timedelta(hours=1))

    qualifying = patients_needing_reading_reminder(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_a_stale_reading_qualifies(make_hospital, patient_with_pregnancy):
    hospital = make_hospital("Stale Reading Reminder Hospital")
    patient = patient_with_pregnancy(hospital)
    stale = timezone.now() - timedelta(days=READING_REMINDER_GAP_DAYS + 1)
    _set(patient, last_reading_at=stale)

    qualifying = patients_needing_reading_reminder(Patient.objects.filter(organization=hospital.org))

    assert patient in qualifying
