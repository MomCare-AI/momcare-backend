"""Clinical rules for maternal risk — the baseline scoring engine.

Deliberately a pure function of readings: no database writes, no side effects,
no framework. That makes it trivially testable, and it makes the seam obvious
when a trained model replaces it — the model has to produce the same shape of
answer, and can be measured against this baseline rather than against nothing.

The thresholds below are the standard obstetric ones. They are **not** a
diagnosis: they say a clinician should look, not what is wrong. Every finding
carries the reading that caused it, because a risk level with no explanation is
not something a clinician can act on or overrule.

⚠ These values should be reviewed by a practising obstetrician before the
system is used in care. They are drawn from widely published thresholds, not
from a clinical authority attached to this project.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

# ── Levels, ordered by severity ──────────────────────────────────────────────
LEVEL_STABLE = "stable"
LEVEL_MODERATE = "moderate"
LEVEL_HIGH = "high"
LEVEL_CRITICAL = "critical"

LEVEL_ORDER = [LEVEL_STABLE, LEVEL_MODERATE, LEVEL_HIGH, LEVEL_CRITICAL]


def highest(levels: list[str]) -> str:
    """The worst of several levels — risk never averages away."""
    return max(levels, key=LEVEL_ORDER.index) if levels else LEVEL_STABLE


# ── Thresholds ───────────────────────────────────────────────────────────────
# Blood pressure: gestational hypertension is ≥140/90; ≥160/110 is severe and
# is the one that matters most, since preeclampsia is the complication this
# system exists to catch early.
BP_SYSTOLIC_MODERATE = Decimal("140")
BP_DIASTOLIC_MODERATE = Decimal("90")
BP_SYSTOLIC_SEVERE = Decimal("160")
BP_DIASTOLIC_SEVERE = Decimal("110")
BP_SYSTOLIC_LOW = Decimal("90")

TEMP_FEVER = Decimal("38.0")
TEMP_HIGH_FEVER = Decimal("39.0")

HR_TACHYCARDIA = Decimal("120")
HR_BRADYCARDIA = Decimal("50")

# How long silence is tolerated before it becomes a finding in its own right.
STALE_AFTER = timedelta(hours=12)

ENGINE_VERSION = "rules-1.0"


@dataclass
class Finding:
    """One reason behind a level. Never a bare verdict."""

    code: str
    level: str
    detail: str
    reading_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "level": self.level,
            "detail": self.detail,
            "reading_id": self.reading_id,
        }


@dataclass
class Assessment:
    level: str
    findings: list[Finding] = field(default_factory=list)
    engine_version: str = ENGINE_VERSION

    @property
    def is_actionable(self) -> bool:
        return self.level != LEVEL_STABLE


def _blood_pressure(reading) -> list[Finding]:
    systolic, diastolic = reading.value, reading.value_secondary
    if diastolic is None:
        return []

    rid = str(reading.id)
    shown = f"{systolic:.0f}/{diastolic:.0f} mmHg"

    if systolic >= BP_SYSTOLIC_SEVERE or diastolic >= BP_DIASTOLIC_SEVERE:
        return [
            Finding(
                code="bp_severe",
                level=LEVEL_CRITICAL,
                detail=f"Severe hypertension — {shown}. Urgent review; possible preeclampsia.",
                reading_id=rid,
            ),
        ]
    if systolic >= BP_SYSTOLIC_MODERATE or diastolic >= BP_DIASTOLIC_MODERATE:
        return [
            Finding(
                code="bp_raised",
                level=LEVEL_MODERATE,
                detail=f"Raised blood pressure — {shown}, at or above 140/90.",
                reading_id=rid,
            ),
        ]
    if systolic < BP_SYSTOLIC_LOW:
        return [
            Finding(
                code="bp_low",
                level=LEVEL_MODERATE,
                detail=f"Low blood pressure — {shown}.",
                reading_id=rid,
            ),
        ]
    return []


def _temperature(reading) -> list[Finding]:
    rid = str(reading.id)
    shown = f"{reading.value:.1f} °C"

    if reading.value >= TEMP_HIGH_FEVER:
        return [
            Finding(
                code="fever_high",
                level=LEVEL_HIGH,
                detail=f"High fever — {shown}. Possible infection.",
                reading_id=rid,
            ),
        ]
    if reading.value >= TEMP_FEVER:
        return [
            Finding(
                code="fever",
                level=LEVEL_MODERATE,
                detail=f"Fever — {shown}.",
                reading_id=rid,
            ),
        ]
    return []


def _heart_rate(reading) -> list[Finding]:
    rid = str(reading.id)
    shown = f"{reading.value:.0f} bpm"

    if reading.value >= HR_TACHYCARDIA:
        return [
            Finding(
                code="tachycardia",
                level=LEVEL_MODERATE,
                detail=f"Raised heart rate — {shown}.",
                reading_id=rid,
            ),
        ]
    if reading.value <= HR_BRADYCARDIA:
        return [
            Finding(
                code="bradycardia",
                level=LEVEL_MODERATE,
                detail=f"Low heart rate — {shown}.",
                reading_id=rid,
            ),
        ]
    return []


EVALUATORS = {
    "blood_pressure": _blood_pressure,
    "temperature": _temperature,
    "heart_rate": _heart_rate,
}


def assess(latest_by_type: dict, *, now, has_any_readings: bool = True) -> Assessment:
    """Judge a pregnancy from its most recent reading of each type.

    ``latest_by_type`` maps reading type to the newest VitalReading of that
    type, exactly as ``services.latest_readings`` returns.

    Stale data is a finding rather than silence. A patient whose band stopped
    reporting twelve hours ago is not stable — she is unobserved, and a system
    that renders that as calm is failing in the way that matters most.
    """
    findings: list[Finding] = []

    for reading_type, reading in latest_by_type.items():
        evaluate = EVALUATORS.get(reading_type)
        if evaluate is not None:
            findings.extend(evaluate(reading))

    if has_any_readings and latest_by_type:
        newest = max(r.recorded_at for r in latest_by_type.values())
        silence = now - newest
        if silence >= STALE_AFTER:
            hours = int(silence.total_seconds() // 3600)
            findings.append(
                Finding(
                    code="stale_readings",
                    level=LEVEL_MODERATE,
                    detail=(
                        f"No readings for {hours} hours — this patient is not "
                        "currently being monitored."
                    ),
                ),
            )

    return Assessment(level=highest([f.level for f in findings]), findings=findings)
