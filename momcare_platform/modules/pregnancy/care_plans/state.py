"""What a care plan is built from, and how a new reading compares with it.

Pure functions only -- no database, no Django -- so the rule that decides whether
a reading is a quick-advice moment, a re-plan, or nothing is testable in
milliseconds and cannot quietly depend on anything else.

**Categories, not raw numbers.** BP 148 then 150 must not look like a change, so
every vital is reduced to its clinical category first (the five that
``momcare_model.clinical_categories`` already defines, plus coarse bands for the
two 0-10 scores). A reading differs from the plan when a category moves, not
when a number does.

A weekly plan is written for a **baseline state**. Each later reading in the week
is compared with it (``assess_change``): a structural change (trimester,
allergies, ...) re-plans at once; a worse state earns quick advice, and a re-plan
only if it persists (see ``services``); the same or a better state does nothing --
loosening waits for the next weekly plan.

NOT reviewed by an obstetrician: the stress/activity bands and the severity
ranking below are product decisions awaiting clinical sign-off, like every other
threshold in this project.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

# The axes a plan reacts to. Order is the display order, and the order the
# state key is built in.
VITAL_AXES = (
    "bp",
    "heart_rate",
    "temperature",
    "glucose",
    "hemoglobin",
    "stress",
    "activity",
)

# Higher = worse. A category absent from here is unknown ("") and never ranked.
_SEVERITY = {
    "bp": {"Normal": 0, "Elevated": 1, "Hypotensive": 1, "Stage 1": 2, "Stage 2": 3, "Hypertensive Crisis": 4},
    "heart_rate": {"Normal": 0, "Bradycardia": 1, "Tachycardia": 1},
    "temperature": {"Normal": 0, "Low": 1, "Low Grade Fever": 1, "Fever": 2, "Hypothermia": 2},
    "glucose": {"Normal": 0, "Prediabetes": 1, "Diabetes": 2},
    "hemoglobin": {"Normal": 0, "Mild Anemia": 1, "Moderate Anemia": 2, "Severe Anemia": 3},
    "stress": {"Low": 0, "Moderate": 1, "High": 2},
    # Low activity is the concerning direction here, not high.
    "activity": {"Moderate": 0, "High": 0, "Low": 1},
}
_RISK_SEVERITY = {"low": 0, "medium": 1, "high": 2}

VITAL_LABELS = {
    "bp": "Blood pressure",
    "heart_rate": "Heart rate",
    "temperature": "Temperature",
    "glucose": "Blood glucose",
    "hemoglobin": "Hemoglobin",
    "stress": "Stress score",
    "activity": "Physical activity score",
}


def category_severity(axis: str, category: str) -> int:
    """0 = normal or not recorded; higher = more severe."""
    return _SEVERITY[axis].get(category, 0)


def risk_severity(risk: str) -> int:
    return _RISK_SEVERITY.get(risk, 0)


def stress_category(score) -> str:
    """0-10 scale. Coarse thirds; the scale's own meaning is not documented anywhere
    in the training notebook, so these bands are a placeholder for clinical review."""
    if score is None:
        return ""
    score = float(score)
    if score <= 3:
        return "Low"
    if score <= 6:
        return "Moderate"
    return "High"


def activity_category(score) -> str:
    """0-10 scale, same caveat as ``stress_category``."""
    if score is None:
        return ""
    score = float(score)
    if score < 3:
        return "Low"
    if score <= 7:
        return "Moderate"
    return "High"


def age_band(age) -> str:
    """Under 18 and 35+ carry their own advice notes; unknown stays unknown."""
    if age is None:
        return ""
    age = int(age)
    if age < 18:
        return "under_18"
    if age >= 35:
        return "35_plus"
    return "18_to_34"


def vitals_from_reading(reading) -> dict[str, str]:
    """Category per axis from one ``VitalReading``-like object ("" when missing)."""
    from momcare_model import clinical_categories as cc  # noqa: PLC0415

    return {
        "bp": cc.bp_category(reading.systolic_bp, reading.diastolic_bp),
        "heart_rate": cc.heart_rate_category(reading.heart_rate),
        "temperature": cc.temperature_category(reading.body_temp_f),
        "glucose": cc.glucose_category(reading.blood_glucose),
        "hemoglobin": cc.hemoglobin_category(reading.hemoglobin),
        "stress": stress_category(reading.stress_score),
        "activity": activity_category(reading.phys_activity_score),
    }


@dataclass(frozen=True)
class CareState:
    """Everything about the *patient* the generator depends on. Two equal states
    must always produce the same ``key`` -- that equality is what "the patient's
    condition is unchanged" means.

    Deliberately excludes a doctor's edits and the hospital's approved
    preferences. Those are passed to the model when it does regenerate, but a
    doctor changing breakfast must not force a regeneration at the next reading
    of an unchanged patient -- the plan she approved stays exactly as approved.
    """

    trimester: int | None
    risk: str
    vitals: dict = field(default_factory=dict)  # axis -> category ("" = unknown)
    region: str = ""
    age_band: str = ""
    allergies: tuple = ()
    dietary_preference: str = ""
    factors: tuple = ()  # pregnancy factors answered "yes"

    def as_dict(self) -> dict:
        return {
            "trimester": self.trimester,
            "risk": self.risk,
            "vitals": {axis: self.vitals.get(axis, "") for axis in VITAL_AXES},
            "region": self.region,
            "age_band": self.age_band,
            "allergies": sorted(self.allergies),
            "dietary_preference": self.dietary_preference,
            "factors": sorted(self.factors),
        }

    @property
    def key(self) -> str:
        return hashlib.sha256(json.dumps(self.as_dict(), sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_dict(cls, data: dict) -> CareState:
        return cls(
            trimester=data.get("trimester"),
            risk=data.get("risk", ""),
            vitals=dict(data.get("vitals", {})),
            region=data.get("region", ""),
            age_band=data.get("age_band", ""),
            allergies=tuple(data.get("allergies", ())),
            dietary_preference=data.get("dietary_preference", ""),
            factors=tuple(data.get("factors", ())),
        )


def carry_forward_unknown_vitals(new_vitals: dict, previous_vitals: dict | None) -> dict:
    """A reading that simply lacks a vital must not flip the plan.

    ``VitalReading`` fields are all nullable, and hemoglobin/glucose in
    particular arrive on their own slower schedule. If the latest reading has
    no hemoglobin, treating that as "now unknown" would drop the iron focus on
    one row and restore it on the next -- exactly the flip-flopping the
    category design exists to prevent. So an axis missing from the latest
    reading keeps the category the current plan was built on. A vital that has
    never been recorded stays unknown ("") -- never guessed.
    """
    merged = dict(new_vitals)
    for axis in VITAL_AXES:
        if not merged.get(axis) and previous_vitals and previous_vitals.get(axis):
            merged[axis] = previous_vitals[axis]
    return merged


def _severity(state: CareState) -> dict[str, int]:
    out = {"risk": _RISK_SEVERITY.get(state.risk, 0)}
    for axis in VITAL_AXES:
        out[axis] = _SEVERITY[axis].get(state.vitals.get(axis, ""), 0)
    return out


def _structural_fields(state: CareState) -> dict:
    """Everything that is not a severity axis. A change here regenerates at once:
    a new trimester changes the advice itself, and a newly recorded allergy must
    never wait two readings to be reflected."""
    d = state.as_dict()
    return {k: d[k] for k in ("trimester", "region", "age_band", "allergies", "dietary_preference", "factors")}


CHANGE_STRUCTURAL = "structural"
CHANGE_WORSE = "worse"
CHANGE_SAME = "same"
CHANGE_BETTER = "better"


def assess_change(baseline: CareState | None, new: CareState) -> str:
    """How ``new`` compares with the state a weekly plan was written for.

    ``structural`` -- something other than severity changed (trimester,
    allergies, dietary preference, pregnancy factors, age band): the plan itself
    no longer fits. ``worse`` -- risk or any vital category is more severe.
    ``same`` -- the identical state. ``better`` -- no more severe anywhere and
    less severe somewhere (or a different category of the same severity).
    """
    if baseline is None:
        return CHANGE_STRUCTURAL
    if new.key == baseline.key:
        return CHANGE_SAME
    if _structural_fields(new) != _structural_fields(baseline):
        return CHANGE_STRUCTURAL
    old_sev, new_sev = _severity(baseline), _severity(new)
    if any(new_sev[axis] > old_sev[axis] for axis in old_sev):
        return CHANGE_WORSE
    return CHANGE_BETTER


def abnormal_axes(state: CareState) -> list[str]:
    """The vital axes currently outside the normal band, most severe first -- the
    "areas to work on" for a weekly plan."""
    severity = _severity(state)
    axes = [axis for axis in VITAL_AXES if severity[axis] > 0]
    return sorted(axes, key=lambda axis: -severity[axis])
