"""Device assignment, risk scoring, and reading statistics."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Avg, Count, Max, Min
from django.utils import timezone
from rest_framework import serializers

from momcare_model import clinical_categories
from momcare_model.statistics import allocate_percentages, round_metric_value
from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.services import month_bounds
from momcare_platform.core.patients.models import Pregnancy
from momcare_platform.modules.pregnancy.vitals.models import Device, RiskAssessment, VitalReading


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

    A row is written for every reading, unconditionally — one
    ``RiskAssessment`` per ``VitalReading``, never re-running the model to
    reconstruct history later. This reverses an earlier "transitions only"
    design (a row only when ``final_risk_level`` changed) — kept the
    pregnancy's full risk history one call away without a second inference,
    but the user explicitly decided the complete per-reading record matters
    more than the smaller table: see MEMORY.md's per-reading risk history
    decision. ``previous_risk_level`` still records what the level was
    immediately before this row, so a transition is always recoverable by
    comparing consecutive rows even though every row is now stored.

    Two postprocessing steps run on the model's raw answer, per the decisions
    in MEMORY.md's ML design doc:

    - Africa + Medium is shown as High. The model was trained region-blind,
      and this hospital's population needs the more cautious reading — see
      ``core.common.regions``.
    - A confidence below this hospital's threshold (``Organization.
      effective_confidence_threshold``, 80% platform default) flags the row
      for review — no email fires for this, by design (see MEMORY.md §"no
      email leg"). The patient still sees the result; a flag means a doctor
      should look again via the Risk Review Queue, not that anything is being
      withheld.

    Scoring and alerting are one transaction: an assessment saying "high"
    with no alert to match is a state this system must not be able to reach.
    """
    from momcare_model.config import FEATURE_COLS  # noqa: PLC0415
    from momcare_model.predict import predict  # noqa: PLC0415
    from momcare_platform.core.common.regions import REGION_AFRICA  # noqa: PLC0415
    from momcare_platform.modules.pregnancy.alerts import services as alert_services  # noqa: PLC0415

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

    return assessment


def current_risk(pregnancy) -> RiskAssessment | None:
    """The standing judgement — the most recent assessment, whatever its level."""
    return pregnancy.risk_assessments.order_by("-assessed_at").first()


# ── Patient-list roster filters ──────────────────────────────────────────────
# ``?workflow=risk_review``/``?workflow=low_confidence`` on GET /api/patients/,
# matching Neuro_RPM's own query-param-on-one-endpoint convention. Root on
# Patient (via the ``pregnancies__`` join), not Pregnancy directly, since the
# caller is always the patient list. Resolved from core.patients via
# importlib, never a static import -- core must not import modules.


def patients_needing_risk_review(patients):
    """Patients with a pending, actionable (Medium/High) assessment on their
    active pregnancy -- same condition ``RiskReviewQueueView`` used before
    the endpoints were consolidated into query params."""
    return patients.filter(
        pregnancies__status=Pregnancy.STATUS_ACTIVE,
        pregnancies__risk_assessments__review_status=RiskAssessment.REVIEW_PENDING,
        pregnancies__risk_assessments__final_risk_level__in=[RiskAssessment.LEVEL_MEDIUM, RiskAssessment.LEVEL_HIGH],
    ).distinct()


def patients_needing_low_confidence_review(patients):
    """Patients with a pending assessment the model itself was unsure about
    -- same condition ``LowConfidenceQueueView`` used before consolidation."""
    return patients.filter(
        pregnancies__status=Pregnancy.STATUS_ACTIVE,
        pregnancies__risk_assessments__review_status=RiskAssessment.REVIEW_PENDING,
        pregnancies__risk_assessments__flagged_for_review=True,
    ).distinct()


NOT_ASSESSED = "not_assessed"
_RISK_SEVERITY = [RiskAssessment.LEVEL_LOW, RiskAssessment.LEVEL_MEDIUM, RiskAssessment.LEVEL_HIGH]


def _empty_risk_buckets() -> dict:
    return {RiskAssessment.LEVEL_LOW: 0, RiskAssessment.LEVEL_MEDIUM: 0, RiskAssessment.LEVEL_HIGH: 0, NOT_ASSESSED: 0}


def risk_level_breakdown(patients) -> dict:
    """How many patients in ``patients`` sit at each risk level, counted two
    ways -- the dashboard's Risk column shows both, so its KPI does too:

    - ``latest``: each patient's most recent ``RiskAssessment`` on an active
      pregnancy (``final_risk_level``, the level actually acted on).
    - ``this_month``: each patient's most common level across the current
      **calendar month** (patient's own location timezone), ties broken
      toward the more severe level -- the same rule
      ``compute_month_risk_breakdown`` uses, so the two can never disagree.

    A patient with nothing to count (no active pregnancy, or no assessment
    yet / none this month) lands in ``not_assessed`` rather than being
    dropped or called Low, so the four buckets always sum to the number of
    patients passed in.
    """
    patient_ids = list(patients.order_by().values_list("pk", flat=True).distinct())
    latest = _empty_risk_buckets()
    this_month = _empty_risk_buckets()

    latest_levels = dict(
        RiskAssessment.objects.filter(
            pregnancy__patient_id__in=patient_ids,
            pregnancy__status=Pregnancy.STATUS_ACTIVE,
        )
        .order_by("pregnancy__patient_id", "-assessed_at")
        .distinct("pregnancy__patient_id")
        .values_list("pregnancy__patient_id", "final_risk_level"),
    )
    for patient_id in patient_ids:
        latest[latest_levels.get(patient_id, NOT_ASSESSED)] += 1

    month_levels: dict = {}
    for location in Location.objects.filter(pk__in=patients.order_by().values("location_id")):
        now_local = timezone.localtime(timezone.now(), timezone=location.timezone)
        start, end = month_bounds(year=now_local.year, month=now_local.month, tzinfo=location.timezone)
        rows = (
            RiskAssessment.objects.filter(
                pregnancy__patient_id__in=patients.filter(location=location).order_by().values("pk"),
                pregnancy__status=Pregnancy.STATUS_ACTIVE,
                assessed_at__gte=start,
                assessed_at__lte=end,
            )
            .order_by()
            .values_list("pregnancy__patient_id", "final_risk_level")
        )
        for patient_id, level in rows:
            month_levels.setdefault(patient_id, Counter())[level] += 1

    for patient_id in patient_ids:
        counts = month_levels.get(patient_id)
        if not counts:
            this_month[NOT_ASSESSED] += 1
            continue
        most_common = max(_RISK_SEVERITY, key=lambda level: (counts[level], _RISK_SEVERITY.index(level)))
        this_month[most_common] += 1

    return {"latest": latest, "this_month": this_month}


# ── Risk review workflow ─────────────────────────────────────────────────────
# Adapted from Neuro_RPM's Reading Review workflow (patients/services.py
# resolve_reading()/review_reading()/escalate_reading()) — same pending ->
# reviewed/escalated shape and the same "already resolved" guard, applied to
# RiskAssessment instead of PatientReading since MomCare's trigger is the
# trained model's confidence, not a configurable DataBound threshold (MomCare
# deliberately has no such rules engine — see MEMORY.md's "no rules engine"
# decision). Unlike Neuro_RPM, "escalated" here is still just a label — no
# Alert side effect — matching what Neuro_RPM's own escalate() actually does
# (nothing beyond the status), a deliberate choice confirmed with the user
# rather than an oversight.


def resolve_risk_review(assessment, *, new_status, confirmed_risk_level, actor) -> RiskAssessment:
    """Move a pending assessment that needs attention to reviewed or escalated.

    Only an assessment that is still Pending **and** worth attention (see
    ``RiskAssessment.needs_attention`` — actionable, flagged for low
    confidence, or both) can be resolved — the same condition the Risk
    Review Queue lists against, so nothing shown there ever dead-ends here.
    Broader than Neuro_RPM's own ``resolve_reading()`` guard (theirs has
    only one axis, out-of-range severity; MomCare has two independent
    signals — severity and model confidence — since only MomCare runs a
    real probabilistic model here). ``confirmed_risk_level`` is required on
    both actions, not optional the way Neuro_RPM's plain review/escalate
    are — MomCare's own deliberate rule (see ``VerifyRiskView``'s former
    docstring): there is no "just seen, not confirmed" state.
    """
    if not assessment.needs_attention:
        raise MonitoringError("Only an actionable or flagged assessment can be reviewed.")
    if assessment.review_status != RiskAssessment.REVIEW_PENDING:
        raise MonitoringError("This assessment has already been resolved.")

    assessment.confirmed_risk_level = confirmed_risk_level
    assessment.review_status = new_status
    assessment.verified_at = timezone.now()
    assessment.verified_by = actor
    assessment.save(
        update_fields=["confirmed_risk_level", "review_status", "verified_at", "verified_by"],
    )
    return assessment


def review_risk(assessment, *, confirmed_risk_level, actor=None) -> RiskAssessment:
    """Mark a pending assessment as reviewed — no further action needed."""
    return resolve_risk_review(
        assessment,
        new_status=RiskAssessment.REVIEW_REVIEWED,
        confirmed_risk_level=confirmed_risk_level,
        actor=actor,
    )


def escalate_risk(assessment, *, confirmed_risk_level, actor=None) -> RiskAssessment:
    """Mark a pending assessment as escalated — a triage label only, exactly
    matching what Neuro_RPM's own ``escalate_reading()`` does: no Alert side
    effect, no notification. MomCare's real escalation ladder (``Alert``/
    ``AlertEvent``) already runs independently of this field."""
    return resolve_risk_review(
        assessment,
        new_status=RiskAssessment.REVIEW_ESCALATED,
        confirmed_risk_level=confirmed_risk_level,
        actor=actor,
    )


@transaction.atomic
def bulk_resolve_risk_reviews(items, *, actor, queryset) -> list[RiskAssessment]:
    """Resolve many pending assessments, each to its own target status, in one
    atomic transaction — ported from Neuro_RPM's ``bulk_resolve_readings()``.

    ``items`` is an iterable of ``(assessment_id, new_status, confirmed_risk_level)``
    triples, ``new_status`` already resolved to ``RiskAssessment.REVIEW_REVIEWED``/
    ``REVIEW_ESCALATED``. An ``assessment_id`` repeated with the same status
    collapses to one write; repeated with a different status is an
    unresolvable conflict and raises before anything is looked up. Any
    failure (not pending, not actionable, or not found in ``queryset`` — the
    caller's own hospital/assignment-scoped access) rolls back every write
    this call made, matching Neuro_RPM's own all-or-nothing behaviour.
    """
    status_by_id: dict[str, str] = {}
    confirmed_by_id: dict[str, str] = {}
    ordered_ids: list[str] = []
    conflicting_ids: list[str] = []
    for assessment_id, new_status, confirmed_risk_level in items:
        aid = str(assessment_id)
        if aid not in status_by_id:
            status_by_id[aid] = new_status
            confirmed_by_id[aid] = confirmed_risk_level
            ordered_ids.append(aid)
        elif status_by_id[aid] != new_status:
            conflicting_ids.append(aid)

    if conflicting_ids:
        msg = [f"Assessment {aid} was given more than one target status." for aid in dict.fromkeys(conflicting_ids)]
        raise MonitoringError(" ".join(msg))

    assessments = list(queryset.filter(pk__in=ordered_ids))
    found_ids = {str(assessment.pk) for assessment in assessments}
    missing_ids = [aid for aid in ordered_ids if aid not in found_ids]
    if missing_ids:
        msg = [f"Assessment not found or not accessible: {aid}" for aid in missing_ids]
        raise MonitoringError(" ".join(msg))

    assessments_by_id = {str(assessment.pk): assessment for assessment in assessments}
    for aid in ordered_ids:
        resolve_risk_review(
            assessments_by_id[aid],
            new_status=status_by_id[aid],
            confirmed_risk_level=confirmed_by_id[aid],
            actor=actor,
        )
    return [assessments_by_id[aid] for aid in ordered_ids]


def latest_readings(pregnancy) -> VitalReading | None:
    """The most recent reading event for this pregnancy, or None if there isn't one.

    A reading event carries every vital known at that moment together (wide
    format), so there is exactly one "latest" — not one per type.
    """
    return VitalReading.objects.filter(pregnancy=pregnancy).order_by("-recorded_at").first()


# ── Reading statistics & vitals summary ─────────────────────────────────────
# See docs/design/2026-09-25-reading-statistics-design.md. Adapted from
# Neuro_RPM's reading-statistics/vitals-summary features, simplified for
# VitalReading's flat, named-column schema (no per-type table split, so no
# weighted cross-table merge and no generic value_1..value_5 remapping).

# Day-count for each preset window, inclusive of today -- "2_days" means today
# and yesterday, not today plus 48 hours.
READING_PERIODS = {
    "2_days": 2,
    "1_week": 7,
    "1_month": 30,
    "3_months": 90,
    "6_months": 180,
    "1_year": 365,
}

# One vital-scoped group per selectable `reading_type` -- statistics are
# always scoped to exactly one group, never all 9 fields at once (averaging
# blood pressure together with glucose would not mean anything).
READING_TYPE_FIELDS = {
    "blood_pressure": ["systolic_bp", "diastolic_bp", "heart_rate"],
    "temperature": ["body_temp_f"],
    "blood_glucose": ["blood_glucose"],
    "hemoglobin": ["hemoglobin"],
    "wellness": ["stress_score", "phys_activity_score"],
}

# Which of the five clinical_categories functions apply to which reading_type.
# "wellness" has none -- no clinical bands are defined for stress/activity scores.
READING_TYPE_CATEGORIES = {
    "blood_pressure": ["bp_category", "heart_rate_category"],
    "temperature": ["temperature_category"],
    "blood_glucose": ["glucose_category"],
    "hemoglobin": ["hemoglobin_category"],
    "wellness": [],
}

# How to classify one raw row (a dict of field values) per category key --
# bp_category is the only one needing two fields from the same row.
_CATEGORY_CLASSIFIERS = {
    "bp_category": lambda row: clinical_categories.bp_category(row.get("systolic_bp"), row.get("diastolic_bp")),
    "heart_rate_category": lambda row: clinical_categories.heart_rate_category(row.get("heart_rate")),
    "temperature_category": lambda row: clinical_categories.temperature_category(row.get("body_temp_f")),
    "glucose_category": lambda row: clinical_categories.glucose_category(row.get("blood_glucose")),
    "hemoglobin_category": lambda row: clinical_categories.hemoglobin_category(row.get("hemoglobin")),
}


def resolve_reading_period(code: str, tzinfo) -> tuple:
    """``(start, end)`` day-boundary bounds for a preset reading-statistics
    window, in the pregnancy's own location timezone -- inclusive of today.
    """
    if code not in READING_PERIODS:
        raise serializers.ValidationError({"period": [f"Must be one of: {', '.join(READING_PERIODS)}."]})
    now_local = timezone.localtime(timezone.now(), timezone=tzinfo)
    end = now_local.replace(hour=23, minute=59, second=59, microsecond=999999)
    start = (now_local - timedelta(days=READING_PERIODS[code] - 1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return start, end


def resolve_custom_reading_range(start_date: str, end_date: str, tzinfo) -> tuple:
    """``(start, end)`` day-boundary bounds for a caller-given ``YYYY-MM-DD``
    range, inclusive both ends, in the pregnancy's own location timezone.
    """
    try:
        start_naive = datetime.strptime(start_date, "%Y-%m-%d")
        end_naive = datetime.strptime(end_date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise serializers.ValidationError(
            {"start_date": ["start_date and end_date must both be given as YYYY-MM-DD."]},
        ) from exc
    if start_naive > end_naive:
        raise serializers.ValidationError({"start_date": ["start_date must not be after end_date."]})

    start = timezone.make_aware(start_naive.replace(hour=0, minute=0, second=0, microsecond=0), tzinfo)
    end = timezone.make_aware(end_naive.replace(hour=23, minute=59, second=59, microsecond=999999), tzinfo)
    return start, end


def compute_reading_statistics(queryset, reading_type: str) -> dict:
    """Average/min/max/count plus category-percentage breakdown for exactly
    one ``reading_type`` group, over an already date-filtered ``queryset``.

    Real DB aggregation (``.aggregate()``), not Neuro_RPM's Python-list
    approach -- that existed only to solve a multi-table field-remapping
    problem VitalReading's flat schema doesn't have.
    """
    if reading_type not in READING_TYPE_FIELDS:
        raise serializers.ValidationError({"reading_type": [f"Must be one of: {', '.join(READING_TYPE_FIELDS)}."]})
    fields = READING_TYPE_FIELDS[reading_type]

    annotations: dict[str, Avg | Min | Max | Count] = {}
    for field in fields:
        annotations[f"{field}__avg"] = Avg(field)
        annotations[f"{field}__min"] = Min(field)
        annotations[f"{field}__max"] = Max(field)
        annotations[f"{field}__count"] = Count(field)
    result = queryset.aggregate(**annotations)

    average: dict[str, float | int] = {}
    minimum: dict[str, float | int] = {}
    maximum: dict[str, float | int] = {}
    readings_count: dict[str, int] = {}
    for field in fields:
        count = result[f"{field}__count"]
        if not count:
            continue
        average[field] = round_metric_value(field, float(result[f"{field}__avg"]))
        minimum[field] = round_metric_value(field, float(result[f"{field}__min"]))
        maximum[field] = round_metric_value(field, float(result[f"{field}__max"]))
        readings_count[field] = count

    categories = {}
    category_keys = READING_TYPE_CATEGORIES[reading_type]
    if category_keys:
        rows = list(queryset.values(*fields))
        for category_key in category_keys:
            classify = _CATEGORY_CLASSIFIERS[category_key]
            tallied = Counter(classify(row) for row in rows)
            tallied.pop("", None)  # blank = that vital missing on that row
            total = sum(tallied.values())
            if not total:
                # No reading in the window carried this vital at all -- skip
                # the category rather than showing an all-zero band set,
                # matching Neuro_RPM's own "_compute_categories skips empty
                # counts" behavior.
                continue
            band_info = clinical_categories.CATEGORY_BANDS[category_key]
            band_counts = {band: tallied.get(band, 0) for band in band_info["bands"]}
            percentages = allocate_percentages(band_counts, total)
            categories[category_key] = {
                "guideline": band_info["guideline"],
                "bands": [
                    {"key": band, "count": band_counts[band], "percentage": percentages[band]}
                    for band in band_info["bands"]
                ],
            }

    return {
        "average": average,
        "min": minimum,
        "max": maximum,
        "readings_count": readings_count,
        "categories": categories,
    }


# The vitals a rolling summary averages -- age/stress/activity excluded (age
# isn't a trend metric; stress/activity have no established clinical unit to
# summarize this way).
VITALS_SUMMARY_FIELDS = ["systolic_bp", "diastolic_bp", "heart_rate", "body_temp_f", "blood_glucose", "hemoglobin"]


def compute_vitals_summary(pregnancy) -> dict:
    """Rolling 30-day average across every vital, one aggregate query.

    ``null`` (``None``), never ``0`` or an omitted key, for a vital with no
    readings in the window -- a fabricated normal-looking value is worse
    than an honest gap, same rule ``LatestReadingsView`` already follows.
    """
    since = timezone.now() - timedelta(days=30)
    annotations = {f"{field}__avg": Avg(field) for field in VITALS_SUMMARY_FIELDS}
    result = VitalReading.objects.filter(pregnancy=pregnancy, recorded_at__gte=since).aggregate(**annotations)

    averages: dict[str, float | int | None] = {}
    for field in VITALS_SUMMARY_FIELDS:
        value = result[f"{field}__avg"]
        averages[field] = round_metric_value(field, float(value)) if value is not None else None
    return {
        "last_30_days_average": averages,
        "risk_this_month": compute_month_risk_breakdown(pregnancy),
    }


def compute_month_risk_breakdown(pregnancy) -> dict | None:
    """Percentage of Low/Medium/High among every ``RiskAssessment`` row this
    pregnancy has for the current **calendar month** (Jan/Feb/Mar, not a
    rolling 30-day window like the rest of this endpoint) -- explicitly
    requested to match, e.g., ``monitoring_seconds_this_month``'s own
    calendar-month convention rather than ``last_30_days_average``'s.

    Straightforward now that ``reassess_risk()`` writes one row per reading
    instead of one row per transition (see its own docstring) -- no need to
    reconstruct anything or re-run the model; this just tallies
    ``final_risk_level`` (the level actually acted on, not the model's raw
    answer) on rows already sitting in the table.

    ``None`` when there were no assessments in the month at all -- "never
    assessed this month" must not be confused with "assessed and found
    every reading Low", same "an honest gap beats a fabricated normal-looking
    value" rule ``compute_vitals_summary`` itself already follows.
    """
    tzinfo = pregnancy.patient.location.timezone
    now_local = timezone.localtime(timezone.now(), timezone=tzinfo)
    start, end = month_bounds(year=now_local.year, month=now_local.month, tzinfo=tzinfo)

    levels = pregnancy.risk_assessments.filter(assessed_at__gte=start, assessed_at__lte=end).values_list(
        "final_risk_level",
        flat=True,
    )
    return _month_breakdown_from_levels(levels)


def _month_breakdown_from_levels(levels) -> dict | None:
    """Shared by the single-pregnancy and the batch (patient list) paths, so
    a row, the Vitals Summary and the KPI can never round or tie-break
    differently. ``None`` when ``levels`` is empty."""
    counts = dict.fromkeys([RiskAssessment.LEVEL_LOW, RiskAssessment.LEVEL_MEDIUM, RiskAssessment.LEVEL_HIGH], 0)
    for level in levels:
        counts[level] += 1

    total = sum(counts.values())
    if not total:
        return None

    # On a tie, break toward the more severe level -- a month split evenly
    # between Low and High must never silently surface as "Low", which is
    # the wrong side to round a genuinely mixed month towards for a doctor
    # scanning for who needs attention. Severity order, not dict/insertion
    # order, decides ties.
    most_common = max(_RISK_SEVERITY, key=lambda level: (counts[level], _RISK_SEVERITY.index(level)))

    return {
        "counts": counts,
        "percentages": allocate_percentages(counts, total),
        "most_common": most_common,
        "total_count": total,
    }


def risk_this_month_for_pregnancies(pregnancies) -> dict:
    """``{pregnancy_id: breakdown or None}`` for a page of pregnancies in one
    query per location timezone, not one per row -- what the patient list
    needs for its monthly Risk column. Same shape and rules as
    ``compute_month_risk_breakdown``."""
    pregnancies = list(pregnancies)
    by_location: dict = {}
    for pregnancy in pregnancies:
        by_location.setdefault(pregnancy.patient.location, []).append(pregnancy.pk)

    levels: dict = {pregnancy.pk: [] for pregnancy in pregnancies}
    for location, pregnancy_ids in by_location.items():
        now_local = timezone.localtime(timezone.now(), timezone=location.timezone)
        start, end = month_bounds(year=now_local.year, month=now_local.month, tzinfo=location.timezone)
        rows = (
            RiskAssessment.objects.filter(pregnancy_id__in=pregnancy_ids, assessed_at__gte=start, assessed_at__lte=end)
            .order_by()
            .values_list("pregnancy_id", "final_risk_level")
        )
        for pregnancy_id, level in rows:
            levels[pregnancy_id].append(level)

    return {pregnancy_id: _month_breakdown_from_levels(rows) for pregnancy_id, rows in levels.items()}
