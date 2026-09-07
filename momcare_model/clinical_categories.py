"""The five clinical labels — display-only, never fed to the model.

Ported verbatim (same thresholds, same label strings) from the training
notebook's Phase 2.8 functions (``DataPre Processing/COLLAB CODE/untitled0.py``)
so training-time and inference-time categorisation cannot drift apart. This
is the single source of truth — nothing else in the codebase may redefine
these thresholds.

Every function returns "" for a missing vital, never a guessed default,
matching the notebook's own NaN-safe convention. Inputs are converted to
``float`` before comparison because production vitals arrive as Django
``Decimal`` fields, and Python raises ``TypeError`` comparing ``Decimal`` to
a bare ``float`` literal directly.
"""

from __future__ import annotations


def bp_category(systolic, diastolic) -> str:
    """ACC/AHA blood pressure categories."""
    if systolic is None or diastolic is None:
        return ""
    systolic, diastolic = float(systolic), float(diastolic)
    if systolic >= 180 or diastolic >= 120:
        return "Hypertensive Crisis"
    if systolic >= 140 or diastolic >= 90:
        return "Stage 2"
    if systolic >= 130 or diastolic >= 80:
        return "Stage 1"
    if systolic >= 120:
        return "Elevated"
    if systolic >= 90:
        return "Normal"
    return "Hypotensive"


def heart_rate_category(heart_rate) -> str:
    """American Heart Association heart-rate categories."""
    if heart_rate is None:
        return ""
    heart_rate = float(heart_rate)
    if heart_rate < 60:
        return "Bradycardia"
    if heart_rate > 100:
        return "Tachycardia"
    return "Normal"


def temperature_category(body_temp_f) -> str:
    """CDC/WHO temperature categories, degrees Fahrenheit."""
    if body_temp_f is None:
        return ""
    body_temp_f = float(body_temp_f)
    if body_temp_f < 95:
        return "Hypothermia"
    if body_temp_f < 97:
        return "Low"
    if body_temp_f < 99:
        return "Normal"
    if body_temp_f < 100.4:
        return "Low Grade Fever"
    return "Fever"


def glucose_category(blood_glucose) -> str:
    """American Diabetes Association glucose categories, mg/dL."""
    if blood_glucose is None:
        return ""
    blood_glucose = float(blood_glucose)
    if blood_glucose < 100:
        return "Normal"
    if blood_glucose < 126:
        return "Prediabetes"
    return "Diabetes"


def hemoglobin_category(hemoglobin) -> str:
    """WHO pregnancy anemia categories, g/dL."""
    if hemoglobin is None:
        return ""
    hemoglobin = float(hemoglobin)
    if hemoglobin < 7:
        return "Severe Anemia"
    if hemoglobin < 10:
        return "Moderate Anemia"
    if hemoglobin < 11:
        return "Mild Anemia"
    return "Normal"


def categorize(vitals: dict) -> dict:
    """All five categories for one reading, keyed by RiskAssessment field name."""
    return {
        "bp_category": bp_category(vitals.get("systolic_bp"), vitals.get("diastolic_bp")),
        "heart_rate_category": heart_rate_category(vitals.get("heart_rate")),
        "temperature_category": temperature_category(vitals.get("body_temp_f")),
        "glucose_category": glucose_category(vitals.get("blood_glucose")),
        "hemoglobin_category": hemoglobin_category(vitals.get("hemoglobin")),
    }
