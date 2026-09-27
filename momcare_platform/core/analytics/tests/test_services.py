"""``recompute_monitoring_analytics`` and ``patients_needing_monitoring_follow_up``
-- pure service-level tests, no HTTP.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from momcare_platform.core.analytics.models import PatientAnalytics
from momcare_platform.core.analytics.services import (
    MONITORING_FOLLOW_UP_MAX_SECONDS,
    patients_needing_monitoring_follow_up,
    recompute_monitoring_analytics,
)
from momcare_platform.core.monitoring.models import MonitoringSession
from momcare_platform.core.monitoring.services import create_combined_monitoring
from momcare_platform.core.patients.models import Patient
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha"):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
        )

    return _make


def test_recompute_sums_this_months_sessions(make_hospital, patient_for):
    hospital = make_hospital("Recompute Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=300,
        recorded_at=now,
        added_by=hospital.admin,
    )
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=400,
        recorded_at=now,
        added_by=hospital.admin,
    )

    row = PatientAnalytics.objects.get(patient=patient, period_month=now.date().replace(day=1))
    assert row.monitoring_seconds == 700


def test_recompute_updates_last_monitoring_contact_at(make_hospital, patient_for):
    hospital = make_hospital("Last Contact Hospital")
    patient = patient_for(hospital)
    now = timezone.now()

    MonitoringSession.objects.create(patient=patient, duration_seconds=300, recorded_at=now, added_by=hospital.admin)

    patient.refresh_from_db()
    assert patient.last_monitoring_contact_at == now


def test_a_backdated_edit_refreshes_both_the_old_and_new_month(make_hospital, patient_for):
    """MonitoringSession is editable and backdatable -- moving a session's
    recorded_at into a different month must not leave the old month's total
    stale.
    """
    hospital = make_hospital("Backdate Hospital")
    patient = patient_for(hospital)
    now = timezone.now()

    session = MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=300,
        recorded_at=now,
        added_by=hospital.admin,
    )
    this_month = now.date().replace(day=1)
    assert PatientAnalytics.objects.get(patient=patient, period_month=this_month).monitoring_seconds == 300

    last_month = now - timedelta(days=32)
    session.recorded_at = last_month
    session.save()

    assert PatientAnalytics.objects.get(patient=patient, period_month=this_month).monitoring_seconds == 0
    last_month_period = last_month.date().replace(day=1)
    assert PatientAnalytics.objects.get(patient=patient, period_month=last_month_period).monitoring_seconds == 300


def test_deleting_a_session_refreshes_the_month_it_belonged_to(make_hospital, patient_for):
    hospital = make_hospital("Delete Session Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    session = MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=500,
        recorded_at=now,
        added_by=hospital.admin,
    )

    session.delete()

    row = PatientAnalytics.objects.get(patient=patient, period_month=now.date().replace(day=1))
    assert row.monitoring_seconds == 0


def test_a_standalone_note_also_updates_last_monitoring_contact_at(make_hospital, patient_for):
    """A note with no duration creates no session (see
    create_combined_monitoring's own docstring) but is still real contact --
    the recompute must fire for MonitoringNote too, not just sessions.
    """
    hospital = make_hospital("Note Only Contact Hospital")
    patient = patient_for(hospital)
    now = timezone.now()

    create_combined_monitoring(
        patient=patient,
        duration_seconds=None,
        recorded_at=now,
        added_by=hospital.admin,
        note_text="Called, discussed swelling.",
    )

    patient.refresh_from_db()
    assert patient.last_monitoring_contact_at == now
    # No session means no monitoring time -- the note alone doesn't satisfy
    # the 20-minute half of the condition.
    row = PatientAnalytics.objects.get(patient=patient, period_month=now.date().replace(day=1))
    assert row.monitoring_seconds == 0


def test_recompute_can_be_called_directly(make_hospital, patient_for):
    hospital = make_hospital("Direct Call Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    MonitoringSession.objects.create(patient=patient, duration_seconds=250, recorded_at=now, added_by=hospital.admin)

    recompute_monitoring_analytics(patient, [now])

    row = PatientAnalytics.objects.get(patient=patient, period_month=now.date().replace(day=1))
    assert row.monitoring_seconds == 250


def test_recompute_recomputes_from_source_not_from_a_delta(make_hospital, patient_for):
    """Two sessions, then editing one's duration -- the total must reflect
    the current sum, not an incremental add/subtract.
    """
    hospital = make_hospital("Full Recompute Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    first = MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=100,
        recorded_at=now,
        added_by=hospital.admin,
    )
    MonitoringSession.objects.create(patient=patient, duration_seconds=200, recorded_at=now, added_by=hospital.admin)

    first.duration_seconds = 900
    first.save()

    row = PatientAnalytics.objects.get(patient=patient, period_month=now.date().replace(day=1))
    assert row.monitoring_seconds == 1100


# ── patients_needing_monitoring_follow_up ────────────────────────────────


def test_a_never_monitored_patient_qualifies_immediately(make_hospital, patient_for):
    hospital = make_hospital("Never Monitored Hospital")
    patient = patient_for(hospital)

    qualifying = patients_needing_monitoring_follow_up(Patient.objects.filter(organization=hospital.org))

    assert patient in qualifying


def test_a_recently_monitored_patient_with_enough_time_does_not_qualify(make_hospital, patient_for):
    hospital = make_hospital("Compliant Hospital")
    patient = patient_for(hospital)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=MONITORING_FOLLOW_UP_MAX_SECONDS,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
    )

    qualifying = patients_needing_monitoring_follow_up(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_enough_time_this_month_excludes_even_with_a_stale_last_contact(make_hospital, patient_for):
    """Both conditions must hold to qualify. If the monthly minutes
    threshold was already met, the patient is excluded even though the
    last contact itself is more than 2 days old -- state set directly
    (bypassing the signal) to decouple the two conditions cleanly, since a
    single real session would set both at once.
    """
    hospital = make_hospital("Enough Time Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    PatientAnalytics.objects.create(
        patient=patient,
        period_month=now.date().replace(day=1),
        monitoring_seconds=MONITORING_FOLLOW_UP_MAX_SECONDS,
    )
    Patient.objects.filter(pk=patient.pk).update(last_monitoring_contact_at=now - timedelta(days=5))

    qualifying = patients_needing_monitoring_follow_up(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_recent_contact_excludes_even_with_not_enough_time_this_month(make_hospital, patient_for):
    """A contact within the last 2 days excludes the patient even though
    this month's minutes are still below threshold -- either condition
    failing is enough to exclude, matching Neuro_RPM's AND semantics.
    """
    hospital = make_hospital("Recent Contact Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    Patient.objects.filter(pk=patient.pk).update(last_monitoring_contact_at=now - timedelta(hours=2))

    qualifying = patients_needing_monitoring_follow_up(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying


def test_recent_contact_and_enough_time_does_not_qualify(make_hospital, patient_for):
    hospital = make_hospital("Fully Compliant Hospital")
    patient = patient_for(hospital)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=MONITORING_FOLLOW_UP_MAX_SECONDS,
        recorded_at=timezone.now() - timedelta(hours=1),
        added_by=hospital.admin,
    )

    qualifying = patients_needing_monitoring_follow_up(Patient.objects.filter(organization=hospital.org))

    assert patient not in qualifying
