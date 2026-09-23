"""Model-level constraints -- fault-injection style: each guard is proven by
showing the corresponding bad state is actually rejected.
"""

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.monitoring.models import ClinicalTag, MonitoringNote, MonitoringSession
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha"):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date()},
        )

    return _make


def test_clinical_tag_requires_exactly_one_scope(make_hospital):
    make_hospital("Neither Scope Hospital")
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClinicalTag.objects.create(name="Urgent")


def test_clinical_tag_rejects_both_scopes(make_hospital):
    hospital = make_hospital("Both Scope Hospital")
    location = ensure_default_location(hospital.org)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClinicalTag.objects.create(name="Urgent", organization=hospital.org, location=location)


def test_clinical_tag_name_unique_per_organization(make_hospital):
    hospital = make_hospital("Tag Org Hospital")
    ClinicalTag.objects.create(name="High Risk", organization=hospital.org)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ClinicalTag.objects.create(name="High Risk", organization=hospital.org)


def test_clinical_tag_same_name_allowed_at_different_hospitals(make_hospital):
    """A hospital's tag vocabulary must not collide with another's."""
    a = make_hospital("Tag Hospital A")
    b = make_hospital("Tag Hospital B")
    ClinicalTag.objects.create(name="High Risk", organization=a.org)
    # Must not raise.
    ClinicalTag.objects.create(name="High Risk", organization=b.org)


def test_monitoring_session_rejects_zero_duration(make_hospital, patient_for):
    hospital = make_hospital("Zero Duration Hospital")
    patient = patient_for(hospital)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MonitoringSession.objects.create(
                patient=patient,
                pregnancy=patient.current_pregnancy,
                duration_seconds=0,
                added_by=hospital.admin,
            )


def test_monitoring_session_rejects_over_24_hours(make_hospital, patient_for):
    hospital = make_hospital("Long Duration Hospital")
    patient = patient_for(hospital)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MonitoringSession.objects.create(
                patient=patient,
                pregnancy=patient.current_pregnancy,
                duration_seconds=86401,
                added_by=hospital.admin,
            )


def test_monitoring_session_allows_no_pregnancy(make_hospital):
    """onboard_patient() allows a patient with no pregnancy at all; a
    session logged about her must not be blocked by that."""
    hospital = make_hospital("No Pregnancy Yet Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Sana", "last_name": "Bibi"},
    )
    assert patient.current_pregnancy is None

    session = MonitoringSession.objects.create(
        patient=patient,
        pregnancy=None,
        duration_seconds=300,
        added_by=hospital.admin,
    )
    assert session.pregnancy is None


def test_monitoring_note_rejects_both_call_outcomes(make_hospital, patient_for):
    hospital = make_hospital("Both Outcomes Hospital")
    patient = patient_for(hospital)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            MonitoringNote.objects.create(
                patient=patient,
                pregnancy=patient.current_pregnancy,
                note="Called and left a voicemail? Or answered? Can't be both.",
                added_by=hospital.admin,
                left_voicemail=True,
                two_way_communication=True,
            )


def test_monitoring_note_standalone_has_no_session(make_hospital, patient_for):
    hospital = make_hospital("Standalone Note Hospital")
    patient = patient_for(hospital)
    note = MonitoringNote.objects.create(
        patient=patient,
        pregnancy=patient.current_pregnancy,
        note="Reminder call left with mother-in-law.",
        added_by=hospital.admin,
    )
    assert note.session is None
