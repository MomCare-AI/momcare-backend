"""Device assignment and reading generation."""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from momcare_platform.core.monitoring import risk_rules
from momcare_platform.core.monitoring.models import Device, RiskAssessment, VitalReading


class MonitoringError(Exception):
    """Raised when a monitoring operation cannot proceed; message is user-safe."""


@transaction.atomic
def assign_device(*, device: Device, pregnancy, acquisition: str = "") -> Device:
    """Put a band on a wrist.

    Assignment is what lets an incoming reading resolve to a patient — the
    device reports its own serial, not whose it is.
    """
    if device.status == Device.STATUS_ASSIGNED and device.assigned_pregnancy_id != pregnancy.id:
        raise MonitoringError("This device is already assigned to another patient.")
    if device.organization_id != pregnancy.patient.location.organization_id:
        # Belt and braces: the API scopes the queryset, but a device crossing
        # hospitals would attach one hospital's readings to another's patient.
        raise MonitoringError("This device belongs to a different hospital.")
    if not pregnancy.is_active:
        raise MonitoringError("Monitoring can only be assigned to an active pregnancy.")

    existing = pregnancy.devices.filter(status=Device.STATUS_ASSIGNED).exclude(pk=device.pk).first()
    if existing:
        raise MonitoringError(
            f"This patient is already wearing device {existing.serial_number}.",
        )

    device.assigned_pregnancy = pregnancy
    device.status = Device.STATUS_ASSIGNED
    device.assigned_at = timezone.now()
    if acquisition:
        device.acquisition = acquisition
    device.save(update_fields=["assigned_pregnancy", "status", "assigned_at", "acquisition", "updated_at"])
    return device


@transaction.atomic
def unassign_device(*, device: Device, status: str = Device.STATUS_RETURNED) -> Device:
    """Take the band back. Readings already collected are never touched — they
    are observations of things that happened."""
    device.assigned_pregnancy = None
    device.assigned_at = None
    device.status = status
    device.save(update_fields=["assigned_pregnancy", "assigned_at", "status", "updated_at"])
    return device


# ── Simulation ───────────────────────────────────────────────────────────────
#
# No wearable hardware exists yet, so readings are generated to exercise the
# whole pipeline — ingestion, charting, and later risk scoring — end to end.
# Every generated row carries source="simulated" so it can never be mistaken
# for a real measurement, which in a monitoring system is exactly the kind of
# quiet error that matters.

# Loosely realistic resting ranges for pregnancy. Not clinical reference
# values — they exist to produce plausible-looking data, and the thresholds
# that decide risk live in the scoring layer, not here.
NORMAL_RANGES = {
    VitalReading.TYPE_BLOOD_PRESSURE: {"systolic": (105, 128), "diastolic": (65, 82)},
    VitalReading.TYPE_HEART_RATE: (72, 96),
    VitalReading.TYPE_TEMPERATURE: (36.4, 37.2),
}

ELEVATED_RANGES = {
    VitalReading.TYPE_BLOOD_PRESSURE: {"systolic": (142, 165), "diastolic": (92, 108)},
    VitalReading.TYPE_HEART_RATE: (104, 124),
    VitalReading.TYPE_TEMPERATURE: (37.9, 38.6),
}


def _dec(value: float, places: str = "0.01") -> Decimal:
    return Decimal(str(round(value, 2))).quantize(Decimal(places))


def _reading_values(reading_type: str, elevated: bool) -> tuple[Decimal, Decimal | None]:
    ranges = ELEVATED_RANGES if elevated else NORMAL_RANGES

    if reading_type == VitalReading.TYPE_BLOOD_PRESSURE:
        band = ranges[reading_type]
        return (
            _dec(random.uniform(*band["systolic"])),
            _dec(random.uniform(*band["diastolic"])),
        )

    low, high = ranges[reading_type]
    return _dec(random.uniform(low, high)), None


@transaction.atomic
def simulate_readings(
    *,
    pregnancy,
    hours: int = 24,
    device: Device | None = None,
    elevated: bool = False,
) -> list[VitalReading]:
    """Generate a plausible recent history for one pregnancy.

    Sampling rhythms differ by design, matching how the measurements are
    actually taken: heart rate and temperature stream from the band, while
    blood pressure comes from a cuff a couple of times a day. That difference
    is the reason readings are stored one per row rather than one per event.

    ``elevated=True`` produces a hypertensive picture, so the risk rules and
    the alert path can be demonstrated without waiting for a patient to
    genuinely deteriorate.
    """
    if hours <= 0:
        raise MonitoringError("Simulation needs a positive number of hours.")

    now = timezone.now()
    start = now - timedelta(hours=hours)
    readings: list[VitalReading] = []

    schedule = [
        (VitalReading.TYPE_HEART_RATE, 30),        # every half hour
        (VitalReading.TYPE_TEMPERATURE, 120),      # every two hours
        (VitalReading.TYPE_BLOOD_PRESSURE, 720),   # twice a day, from a cuff
    ]

    for reading_type, interval_minutes in schedule:
        moment = start
        while moment <= now:
            value, secondary = _reading_values(reading_type, elevated)
            readings.append(
                VitalReading(
                    pregnancy=pregnancy,
                    reading_type=reading_type,
                    value=value,
                    value_secondary=secondary,
                    recorded_at=moment,
                    source=VitalReading.SOURCE_SIMULATED,
                    device=device,
                ),
            )
            moment += timedelta(minutes=interval_minutes)

    VitalReading.objects.bulk_create(readings)
    # Score once at the end rather than per reading: the rules look only at the
    # latest value of each type, so intermediate assessments would be noise.
    reassess_risk(pregnancy)
    return readings


@transaction.atomic
def reassess_risk(pregnancy) -> RiskAssessment | None:
    """Score a pregnancy from its latest readings, recording only real changes.

    Returns the new assessment, or None when the level is unchanged — writing a
    row per reading would produce millions of near-identical records and bury
    the transitions that actually matter.

    Runs synchronously after each reading. That is affordable because the rules
    read only the latest value of each type, and it means a dangerous reading is
    scored the moment it arrives rather than whenever a scheduler next wakes.
    """
    latest = latest_readings(pregnancy)
    if not latest:
        return None

    result = risk_rules.assess(latest, now=timezone.now())

    previous = pregnancy.risk_assessments.order_by("-assessed_at").first()
    if previous is not None and previous.level == result.level:
        return None

    assessment = RiskAssessment.objects.create(
        pregnancy=pregnancy,
        level=result.level,
        findings=[f.as_dict() for f in result.findings],
        source=RiskAssessment.SOURCE_RULES,
        engine_version=result.engine_version,
        previous_level=previous.level if previous else "",
    )

    # Scoring and alerting are one transaction: an assessment that says
    # "critical" without the alert that tells somebody is the state this
    # system must never be able to reach.
    #
    # Imported here because alerts imports monitoring for RiskAssessment; a
    # module-level import the other way would close the cycle.
    from momcare_platform.core.alerts.services import sync_alert_for  # noqa: PLC0415

    sync_alert_for(assessment)
    return assessment


def current_risk(pregnancy) -> RiskAssessment | None:
    """The standing judgement — the most recent assessment, whatever its level."""
    return pregnancy.risk_assessments.order_by("-assessed_at").first()


def latest_readings(pregnancy) -> dict[str, VitalReading]:
    """The most recent reading of each type, for the patient header.

    Returns only what exists — a missing type stays missing rather than
    defaulting to a normal-looking value, because a dashboard that looks calm
    because data stopped arriving is the worst failure this system could have.
    """
    latest: dict[str, VitalReading] = {}
    for reading_type, _label in VitalReading.TYPE_CHOICES:
        reading = (
            VitalReading.objects.filter(pregnancy=pregnancy, reading_type=reading_type)
            .order_by("-recorded_at")
            .first()
        )
        if reading is not None:
            latest[reading_type] = reading
    return latest
