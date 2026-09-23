"""Calendar-month duration totals -- ``month_bounds``/``format_duration``/
``monitoring_period_totals``/``monitoring_month_totals``, the same shape as
Neuro_RPM's own totals machinery, minus the RPM/CCM/OOR split.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from momcare_platform.core.monitoring.models import MonitoringSession
from momcare_platform.core.monitoring.services import (
    format_duration,
    monitoring_month_totals,
    monitoring_period_totals,
    month_bounds,
)
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


# ── format_duration ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (5, "0m 5s"),
        (65, "1m 5s"),
        (3665, "1h 1m 5s"),
        (90065, "1d 1h 1m 5s"),
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


# ── month_bounds ─────────────────────────────────────────────────────────


def test_month_bounds_spans_the_whole_calendar_month():
    start, end = month_bounds(year=2026, month=2, tzinfo=ZoneInfo("UTC"))
    assert (start.year, start.month, start.day, start.hour, start.minute) == (2026, 2, 1, 0, 0)
    assert (end.year, end.month, end.day, end.hour, end.minute) == (2026, 2, 28, 23, 59)


def test_month_bounds_handles_a_leap_february():
    start, end = month_bounds(year=2028, month=2, tzinfo=ZoneInfo("UTC"))
    assert end.day == 29


# ── monitoring_period_totals / monitoring_month_totals ──────────────────


def test_monitoring_period_totals_sums_sessions_in_range(make_hospital, patient_for):
    hospital = make_hospital("Totals Range Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    MonitoringSession.objects.create(patient=patient, duration_seconds=300, added_by=hospital.admin, recorded_at=now)
    MonitoringSession.objects.create(patient=patient, duration_seconds=600, added_by=hospital.admin, recorded_at=now)

    totals = monitoring_period_totals(patient=patient, start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    assert totals == {"total_seconds": 900, "total_formatted": "15m 0s"}


def test_monitoring_period_totals_excludes_sessions_outside_range(make_hospital, patient_for):
    hospital = make_hospital("Totals Exclude Hospital")
    patient = patient_for(hospital)
    now = timezone.now()
    MonitoringSession.objects.create(patient=patient, duration_seconds=300, added_by=hospital.admin, recorded_at=now)
    outside = now - timedelta(days=40)
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=86400,
        added_by=hospital.admin,
        recorded_at=outside,
    )

    totals = monitoring_period_totals(patient=patient, start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    assert totals["total_seconds"] == 300


def test_monitoring_period_totals_zero_when_nothing_logged(make_hospital, patient_for):
    hospital = make_hospital("Totals Zero Hospital")
    patient = patient_for(hospital)
    now = timezone.now()

    totals = monitoring_period_totals(patient=patient, start=now - timedelta(hours=1), end=now + timedelta(hours=1))

    assert totals == {"total_seconds": 0, "total_formatted": "0m 0s"}


def test_monitoring_month_totals_matches_the_patients_location_timezone(make_hospital, patient_for):
    hospital = make_hospital("Totals Timezone Hospital")
    patient = patient_for(hospital)
    patient.location.timezone = ZoneInfo("Asia/Karachi")
    patient.location.save(update_fields=["timezone"])

    # 2026-03-01 00:30 in Asia/Karachi (UTC+5) is 2026-02-28 19:30 UTC --
    # this session belongs to March in the patient's own timezone even
    # though its UTC timestamp is still in February.
    recorded_at = datetime(2026, 2, 28, 19, 30, tzinfo=ZoneInfo("UTC"))
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=120,
        added_by=hospital.admin,
        recorded_at=recorded_at,
    )

    february_totals = monitoring_month_totals(patient=patient, year=2026, month=2)
    march_totals = monitoring_month_totals(patient=patient, year=2026, month=3)

    assert february_totals["total_seconds"] == 0
    assert march_totals["total_seconds"] == 120


def test_monitoring_month_totals_only_counts_the_requested_month(make_hospital, patient_for):
    hospital = make_hospital("Totals Month Boundary Hospital")
    patient = patient_for(hospital)

    april_recorded_at = datetime(2026, 4, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=300,
        added_by=hospital.admin,
        recorded_at=april_recorded_at,
    )
    march_recorded_at = datetime(2026, 3, 15, 12, 0, tzinfo=ZoneInfo("UTC"))
    MonitoringSession.objects.create(
        patient=patient,
        duration_seconds=700,
        added_by=hospital.admin,
        recorded_at=march_recorded_at,
    )

    april_totals = monitoring_month_totals(patient=patient, year=2026, month=4)
    march_totals = monitoring_month_totals(patient=patient, year=2026, month=3)

    assert april_totals["total_seconds"] == 300
    assert march_totals["total_seconds"] == 700
