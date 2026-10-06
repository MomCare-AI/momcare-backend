"""The rule that decides whether a reading changes a patient's plan.

Pure functions, so these run without a database. Every guarantee here is
asserted with a case that would fail if the rule were removed.
"""

from types import SimpleNamespace
from typing import Any

import pytest

from momcare_platform.modules.pregnancy.care_plans.state import (
    CHANGE_BETTER,
    CHANGE_SAME,
    CHANGE_STRUCTURAL,
    CHANGE_WORSE,
    CareState,
    abnormal_axes,
    activity_category,
    age_band,
    assess_change,
    carry_forward_unknown_vitals,
    stress_category,
    vitals_from_reading,
)


def state(**overrides) -> CareState:
    base: dict[str, Any] = {
        "trimester": 2,
        "risk": "medium",
        "vitals": {"bp": "Normal", "hemoglobin": "Normal", "glucose": "Normal"},
        "region": "asia",
        "age_band": "18_to_34",
    }
    base.update(overrides)
    return CareState(**base)


# ── categories ────────────────────────────────────────────────────────────


def test_a_small_change_inside_one_category_does_not_change_the_state_key():
    a = vitals_from_reading(
        SimpleNamespace(
            systolic_bp=148,
            diastolic_bp=92,
            heart_rate=80,
            body_temp_f=98.2,
            blood_glucose=90,
            hemoglobin=12,
            stress_score=2,
            phys_activity_score=5,
        )
    )
    b = vitals_from_reading(
        SimpleNamespace(
            systolic_bp=152,
            diastolic_bp=95,
            heart_rate=84,
            body_temp_f=98.6,
            blood_glucose=95,
            hemoglobin=12.4,
            stress_score=3,
            phys_activity_score=6,
        )
    )
    assert a == b  # 148 vs 152 is Stage 2 either way


def test_a_missing_vital_is_unknown_never_a_normal_looking_default():
    cats = vitals_from_reading(
        SimpleNamespace(
            systolic_bp=None,
            diastolic_bp=None,
            heart_rate=None,
            body_temp_f=None,
            blood_glucose=None,
            hemoglobin=None,
            stress_score=None,
            phys_activity_score=None,
        )
    )
    assert set(cats.values()) == {""}


@pytest.mark.parametrize(
    ("score", "expected"), [(None, ""), (0, "Low"), (3, "Low"), (3.1, "Moderate"), (6, "Moderate"), (7, "High")]
)
def test_stress_bands(score, expected):
    assert stress_category(score) == expected


@pytest.mark.parametrize(
    ("score", "expected"), [(None, ""), (2.9, "Low"), (3, "Moderate"), (7, "Moderate"), (8, "High")]
)
def test_activity_bands(score, expected):
    assert activity_category(score) == expected


@pytest.mark.parametrize(
    ("age", "expected"), [(None, ""), (17, "under_18"), (18, "18_to_34"), (34, "18_to_34"), (35, "35_plus")]
)
def test_age_bands(age, expected):
    assert age_band(age) == expected


def test_equal_states_have_equal_keys_regardless_of_input_order():
    a = state(allergies=("peanut", "egg"), factors=("diabetes", "chronic_hypertension"))
    b = state(allergies=("egg", "peanut"), factors=("chronic_hypertension", "diabetes"))
    assert a.key == b.key


def test_state_round_trips_through_its_stored_dict():
    original = state(allergies=("egg",), factors=("diabetes",))
    assert CareState.from_dict(original.as_dict()).key == original.key


# ── carry-forward ─────────────────────────────────────────────────────────


def test_a_vital_missing_from_the_latest_reading_keeps_the_plans_category():
    merged = carry_forward_unknown_vitals(
        {"bp": "Stage 2", "hemoglobin": ""}, {"bp": "Normal", "hemoglobin": "Mild Anemia"}
    )
    assert merged["hemoglobin"] == "Mild Anemia"
    assert merged["bp"] == "Stage 2"  # a present value always wins


def test_a_vital_never_recorded_stays_unknown():
    assert carry_forward_unknown_vitals({"hemoglobin": ""}, None)["hemoglobin"] == ""
    assert carry_forward_unknown_vitals({"hemoglobin": ""}, {"hemoglobin": ""})["hemoglobin"] == ""


# ── how a reading compares with the state a plan was written for ─────────


def test_no_baseline_is_a_structural_change():
    assert assess_change(None, state()) == CHANGE_STRUCTURAL


def test_the_identical_state_is_the_same():
    assert assess_change(state(), state()) == CHANGE_SAME


def test_a_worse_vital_is_worse():
    assert (
        assess_change(state(), state(vitals={"bp": "Stage 2", "hemoglobin": "Normal", "glucose": "Normal"}))
        == CHANGE_WORSE
    )


def test_a_higher_risk_level_is_worse():
    assert assess_change(state(risk="medium"), state(risk="high")) == CHANGE_WORSE


def test_one_worse_axis_makes_it_worse_even_when_another_improved():
    baseline = state(vitals={"bp": "Stage 2", "hemoglobin": "Mild Anemia"})
    new = state(vitals={"bp": "Normal", "hemoglobin": "Severe Anemia"})
    assert assess_change(baseline, new) == CHANGE_WORSE


def test_a_lower_risk_level_is_better():
    assert assess_change(state(risk="high"), state(risk="medium")) == CHANGE_BETTER


def test_a_better_vital_with_nothing_worse_is_better():
    baseline = state(vitals={"bp": "Stage 2"})
    assert assess_change(baseline, state(vitals={"bp": "Normal"})) == CHANGE_BETTER


@pytest.mark.parametrize(
    "change",
    [
        {"trimester": 3},
        {"allergies": ("egg",)},
        {"dietary_preference": "vegetarian"},
        {"factors": ("diabetes",)},
        {"age_band": "35_plus"},
    ],
)
def test_a_structural_change_is_structural_whatever_the_severity(change):
    assert assess_change(state(), state(**change)) == CHANGE_STRUCTURAL


def test_a_structural_change_wins_over_a_worse_state():
    assert assess_change(state(), state(risk="high", allergies=("egg",))) == CHANGE_STRUCTURAL


def test_the_abnormal_axes_are_listed_most_severe_first():
    s = state(vitals={"bp": "Hypertensive Crisis", "hemoglobin": "Mild Anemia", "glucose": "Normal", "stress": "High"})
    assert abnormal_axes(s) == ["bp", "stress", "hemoglobin"]


def test_a_state_with_nothing_abnormal_has_no_axes_to_work_on():
    assert abnormal_axes(state(vitals={"bp": "Normal", "hemoglobin": "Normal"})) == []


def test_the_state_has_no_place_for_doctor_edits_so_they_cannot_force_a_regeneration():
    # A doctor changing breakfast must not regenerate the plan at the next
    # reading of an unchanged patient: the edits are not part of the state.
    assert "adjustments" not in state().as_dict()
    assert "preferences" not in state().as_dict()
