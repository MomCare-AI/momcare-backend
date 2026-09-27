"""Monitoring Follow-up care activity -- the first consumer of
``PatientAnalytics``. See that model's own docstring, and
``core.monitoring.signals``/``modules.pregnancy.vitals.signals`` for how the
cache is kept correct.

Thresholds match Neuro_RPM's own ``monitoring_follow_up`` Care Activity
exactly (``core.patients.services.MONITORING_FOLLOW_UP_MAX_SECONDS`` /
``MONITORING_FOLLOW_UP_MIN_GAP_DAYS`` in that codebase) -- used here as a
plain "meaningful contact this month" heuristic, not a billing computation.
MomCare has no billing module and no RPM/CCM program split to reintroduce;
the 20-minute figure is borrowed only as a number, never as a CPT
requirement.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.db.models import F, Max, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from momcare_platform.core.analytics.models import PatientAnalytics
from momcare_platform.core.monitoring.models import MonitoringNote, MonitoringSession
from momcare_platform.core.monitoring.services import monitoring_period_totals, month_bounds
from momcare_platform.core.patients.models import Patient

MONITORING_FOLLOW_UP_MAX_SECONDS = 20 * 60
MONITORING_FOLLOW_UP_MIN_GAP_DAYS = 2
# Matches Neuro_RPM's own unseen_readings/reading_reminder Care Activities.
# Neither depends on DataBound (the deleted rules engine), unlike the
# rejected "Out of Range" Care Activity -- see CLAUDE.md's "Care Activities"
# section for why that one was turned down.
UNSEEN_READINGS_WINDOW_DAYS = 7
READING_REMINDER_GAP_DAYS = 3


def _period_month(moment, *, tzinfo) -> date:
    return timezone.localtime(moment, timezone=tzinfo).date().replace(day=1)


def recompute_monitoring_analytics(patient: Patient, moments: list) -> None:
    """Recompute ``PatientAnalytics.monitoring_seconds`` (for every month
    touched) and ``Patient.last_monitoring_contact_at``, from source data.

    ``moments`` is every ``recorded_at`` value that could plausibly need its
    month's row refreshed -- the session/note's current value, and (on an
    edit) its value before the edit, so a backdated change that moves a
    record across a month boundary refreshes both months, not just one.
    Always a full recompute, never an incremental delta -- MonitoringSession/
    MonitoringNote are editable and backdatable, unlike VitalReading, so a
    delta could silently drift from the truth.
    """
    tzinfo = patient.location.timezone
    periods = {_period_month(moment, tzinfo=tzinfo) for moment in moments if moment is not None}
    for period in periods:
        start, end = month_bounds(year=period.year, month=period.month, tzinfo=tzinfo)
        total = monitoring_period_totals(patient=patient, start=start, end=end)["total_seconds"]
        PatientAnalytics.objects.update_or_create(
            patient=patient,
            period_month=period,
            defaults={"monitoring_seconds": total},
        )

    last_session = MonitoringSession.objects.filter(patient=patient).aggregate(last=Max("recorded_at"))["last"]
    last_note = MonitoringNote.objects.filter(patient=patient).aggregate(last=Max("recorded_at"))["last"]
    candidates = [moment for moment in (last_session, last_note) if moment is not None]
    latest = max(candidates) if candidates else None
    if patient.last_monitoring_contact_at != latest:
        Patient.objects.filter(pk=patient.pk).update(last_monitoring_contact_at=latest)


def patients_needing_monitoring_follow_up(patients: QuerySet) -> QuerySet:
    """Filters ``patients`` down to those matching Neuro_RPM's own
    ``monitoring_follow_up`` condition exactly: less than 20 minutes of
    monitoring time logged this calendar month, AND no monitoring contact
    (session or note) in the last 2 days. Both must hold -- either alone
    doesn't qualify.

    "This calendar month" is resolved once, in UTC, for the whole query --
    a deliberate simplification against Neuro_RPM's own per-Location-
    timezone month boundary (which the recompute side above still honors
    when writing each row's ``period_month``). The two can disagree only
    within the narrow window around midnight UTC on the first/last day of a
    month, for a patient whose Location is many hours away from UTC --
    accepted rather than solved, since nothing here is safety-critical the
    way gestational age or region are.

    A patient with no ``PatientAnalytics`` row yet for the current month
    implicitly has 0 seconds -- never a reason to exclude her, matching
    Neuro_RPM's own "missing row means she qualifies" behavior.
    """
    now = timezone.now()
    current_period = now.date().replace(day=1)
    gap_threshold = now - timedelta(days=MONITORING_FOLLOW_UP_MIN_GAP_DAYS)

    current_month_seconds = PatientAnalytics.objects.filter(
        patient=OuterRef("pk"),
        period_month=current_period,
    ).values("monitoring_seconds")[:1]

    return patients.annotate(_current_month_seconds=Subquery(current_month_seconds)).filter(
        Q(_current_month_seconds__lt=MONITORING_FOLLOW_UP_MAX_SECONDS) | Q(_current_month_seconds__isnull=True),
        Q(last_monitoring_contact_at__lt=gap_threshold) | Q(last_monitoring_contact_at__isnull=True),
    )


def patients_with_unseen_readings(patients: QuerySet) -> QuerySet:
    """Filters ``patients`` down to those with a reading that arrived after
    the last monitoring contact, within the last 7 days -- matches
    Neuro_RPM's own ``unseen_readings`` Care Activity. A patient with no
    reading at all never qualifies (there is nothing to have missed); one
    contacted after her most recent reading also doesn't (a human already
    looked at the state that produced it).
    """
    cutoff = timezone.now() - timedelta(days=UNSEEN_READINGS_WINDOW_DAYS)
    return patients.filter(
        last_reading_at__isnull=False,
        last_reading_at__gte=cutoff,
    ).filter(Q(last_monitoring_contact_at__isnull=True) | Q(last_reading_at__gt=F("last_monitoring_contact_at")))


def patients_needing_reading_reminder(patients: QuerySet) -> QuerySet:
    """Filters ``patients`` down to those with no reading in the last 3
    days -- matches Neuro_RPM's own ``reading_reminder`` Care Activity. A
    patient who has never had a reading at all qualifies immediately, same
    "missing means she qualifies" reasoning as Monitoring Follow-up.
    """
    cutoff = timezone.now() - timedelta(days=READING_REMINDER_GAP_DAYS)
    return patients.filter(Q(last_reading_at__isnull=True) | Q(last_reading_at__lt=cutoff))
