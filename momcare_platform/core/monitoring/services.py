"""Device assignment and risk scoring."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

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

@transaction.atomic
def reassess_risk(pregnancy) -> RiskAssessment | None:
    """Score a pregnancy from its latest reading via the trained model.

    ``momcare_model.predict()`` is the only thing that may ever produce a
    risk_level here — see MEMORY.md's "no rules engine" decision. Returns
    None, writing nothing, in two cases that are not errors: there is no
    reading yet, or ``predict()`` itself refused (every vital on the latest
    reading is missing — see its own docstring).

    A new row is written only when ``final_risk_level`` changed from the
    pregnancy's last assessment — this is a history of transitions, not one
    row per reading. The very first assessment for a pregnancy always
    counts as a transition, since there was no prior level to match.

    Two postprocessing steps run on the model's raw answer, per the decisions
    in MEMORY.md's ML design doc:

    - Africa + Medium is shown as High. The model was trained region-blind,
      and this hospital's population needs the more cautious reading — see
      ``core.common.regions``.
    - A confidence below this hospital's threshold (``Organization.
      effective_confidence_threshold``, 70% platform default) flags the row
      for review and emails the assigned clinician only — never the patient.
      The patient still sees the result; a flag means a doctor should look
      again, not that anything is being withheld.

    Scoring and alerting are one transaction: an assessment saying "high"
    with no alert to match is a state this system must not be able to reach.
    """
    from momcare_model.config import FEATURE_COLS  # noqa: PLC0415
    from momcare_model.predict import predict  # noqa: PLC0415
    from momcare_platform.core.alerts import services as alert_services  # noqa: PLC0415
    from momcare_platform.core.common.regions import REGION_AFRICA  # noqa: PLC0415

    reading = latest_readings(pregnancy)
    if reading is None:
        return None

    vitals = {col: getattr(reading, col) for col in FEATURE_COLS}
    result = predict(vitals)
    if result is None:
        return None

    final_risk_level = result["risk_level"]
    if pregnancy.region == REGION_AFRICA and result["risk_level"] == RiskAssessment.LEVEL_MEDIUM:
        final_risk_level = RiskAssessment.LEVEL_HIGH

    previous = current_risk(pregnancy)
    previous_final_level = previous.final_risk_level if previous else ""
    if previous is not None and previous_final_level == final_risk_level:
        return None

    threshold = pregnancy.patient.location.organization.effective_confidence_threshold
    confidence = Decimal(str(round(result["confidence"], 3)))
    flagged_for_review = confidence < threshold

    assessment = RiskAssessment.objects.create(
        pregnancy=pregnancy,
        reading=reading,
        risk_level=result["risk_level"],
        final_risk_level=final_risk_level,
        previous_risk_level=previous_final_level,
        confidence=confidence,
        flagged_for_review=flagged_for_review,
        bp_category=result["bp_category"],
        heart_rate_category=result["heart_rate_category"],
        temperature_category=result["temperature_category"],
        glucose_category=result["glucose_category"],
        hemoglobin_category=result["hemoglobin_category"],
    )

    alert_services.sync_alert_for(assessment)
    if flagged_for_review:
        alert_services.notify_low_confidence(assessment)

    return assessment


def current_risk(pregnancy) -> RiskAssessment | None:
    """The standing judgement — the most recent assessment, whatever its level."""
    return pregnancy.risk_assessments.order_by("-assessed_at").first()


def latest_readings(pregnancy) -> VitalReading | None:
    """The most recent reading event for this pregnancy, or None if there isn't one.

    A reading event carries every vital known at that moment together (wide
    format), so there is exactly one "latest" — not one per type.
    """
    return VitalReading.objects.filter(pregnancy=pregnancy).order_by("-recorded_at").first()
