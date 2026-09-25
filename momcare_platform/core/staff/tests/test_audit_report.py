"""``resolve_audit_period``/``compute_audit_report`` -- pure aggregation
logic behind the staff audit report, tested independent of the API layer.
"""

import datetime

import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from momcare_platform.core.monitoring.models import MonitoringNote, MonitoringSession
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.staff.services import AUDIT_PERIODS, compute_audit_report, resolve_audit_period

pytestmark = pytest.mark.django_db


# ── resolve_audit_period ──────────────────────────────────────────────────


@pytest.mark.parametrize(("code", "days"), list(AUDIT_PERIODS.items()))
def test_resolve_audit_period_spans_the_expected_number_of_days(code, days):
    start, end = resolve_audit_period(code)

    assert (end - start).days == days


def test_resolve_audit_period_rejects_an_unknown_code():
    with pytest.raises(serializers.ValidationError):
        resolve_audit_period("fortnight")


# ── compute_audit_report ──────────────────────────────────────────────────


def _make_patient(hospital, staff_user, *, first_name="Ayesha"):
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": first_name, "last_name": "Bibi"},
        pregnancy_data={"lmp": datetime.date(2026, 1, 1), "nurse": staff_user.staff},
    )


def _make_alert(pregnancy, staff_user, *, status, acknowledge=True, resolve=False):
    # Resolved via the app registry, not a static import: both live in
    # momcare_platform.modules, which core (and its tests) must never import --
    # the "core must not import modules" import-linter contract.
    from django.apps import apps as django_apps  # noqa: PLC0415

    Alert = django_apps.get_model("alerts", "Alert")  # noqa: N806
    RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")  # noqa: N806

    assessment = RiskAssessment.objects.create(pregnancy=pregnancy, risk_level="high", final_risk_level="high")
    now = timezone.now()
    return Alert.objects.create(
        pregnancy=pregnancy,
        assessment=assessment,
        level="high",
        status=status,
        acknowledged_by=staff_user if acknowledge else None,
        acknowledged_at=now if acknowledge else None,
        resolved_by=staff_user if resolve else None,
        resolved_at=now if resolve else None,
        resolution="handled" if resolve else "",
    )


def test_total_patients_reflects_current_caseload(make_hospital, make_staff):
    hospital = make_hospital("Caseload Report Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@caseloadreport.test")
    _make_patient(hospital, nurse)
    start, end = resolve_audit_period("month")

    report = compute_audit_report(nurse.staff, start=start, end=end)

    assert report["total_patients"] == 1


def test_monitoring_time_totals_and_distribution(make_hospital, make_staff):
    hospital = make_hospital("Monitoring Time Report Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@monitortimereport.test")
    patient = _make_patient(hospital, nurse)
    now = timezone.now()
    MonitoringSession.objects.create(patient=patient, duration_seconds=600, recorded_at=now, added_by=nurse)
    MonitoringSession.objects.create(patient=patient, duration_seconds=300, recorded_at=now, added_by=nurse)
    start, end = resolve_audit_period("month")

    report = compute_audit_report(nurse.staff, start=start, end=end)

    assert report["monitoring_time"]["total_seconds"] == 900
    assert len(report["monitoring_time"]["distribution"]) == 1
    assert report["monitoring_time"]["distribution"][0]["seconds"] == 900


def test_monitoring_time_excludes_another_staff_members_sessions(make_hospital, make_staff):
    hospital = make_hospital("Isolation Report Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@isolationreport.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, "other@isolationreport.test")
    patient = _make_patient(hospital, nurse)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=600,
        recorded_at=timezone.now(),
        added_by=other_nurse,
    )
    start, end = resolve_audit_period("month")

    report = compute_audit_report(nurse.staff, start=start, end=end)

    assert report["monitoring_time"]["total_seconds"] == 0


def test_call_outcomes_counts_two_way_and_voicemail_separately(make_hospital, make_staff):
    hospital = make_hospital("Call Outcomes Report Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@calloutcomesreport.test")
    patient = _make_patient(hospital, nurse)
    now = timezone.now()
    MonitoringNote.objects.create(
        patient=patient,
        note="Reached her.",
        recorded_at=now,
        added_by=nurse,
        two_way_communication=True,
    )
    MonitoringNote.objects.create(
        patient=patient,
        note="Left a message.",
        recorded_at=now,
        added_by=nurse,
        left_voicemail=True,
    )
    MonitoringNote.objects.create(
        patient=patient,
        note="Reached her again.",
        recorded_at=now,
        added_by=nurse,
        two_way_communication=True,
    )
    start, end = resolve_audit_period("month")

    report = compute_audit_report(nurse.staff, start=start, end=end)

    assert report["call_outcomes"] == {
        "two_way_count": 2,
        "voicemail_count": 1,
        "call_success_rate": round(2 / 3, 2),
    }


def test_call_success_rate_is_zero_with_no_calls_logged(make_hospital, make_staff):
    hospital = make_hospital("No Calls Report Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@nocallsreport.test")
    start, end = resolve_audit_period("month")

    report = compute_audit_report(nurse.staff, start=start, end=end)

    assert report["call_outcomes"] == {"two_way_count": 0, "voicemail_count": 0, "call_success_rate": 0}


def test_alerts_handled_counts_acknowledged_and_resolved(make_hospital, make_staff):
    hospital = make_hospital("Alerts Handled Report Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@alertsreport.test")
    # Two separate pregnancies -- Alert enforces at most one *live* alert per
    # pregnancy, so two independently-acted-on alerts need two episodes.
    patient_a = _make_patient(hospital, nurse, first_name="Ayesha")
    patient_b = _make_patient(hospital, nurse, first_name="Sana")
    _make_alert(patient_a.current_pregnancy, nurse, status="acknowledged", acknowledge=True, resolve=False)
    _make_alert(patient_b.current_pregnancy, nurse, status="resolved", acknowledge=True, resolve=True)
    start, end = resolve_audit_period("month")

    report = compute_audit_report(nurse.staff, start=start, end=end)

    assert report["alerts_handled"] == {"acknowledged_count": 2, "resolved_count": 1}
