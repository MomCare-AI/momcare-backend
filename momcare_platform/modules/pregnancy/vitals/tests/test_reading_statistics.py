"""``resolve_reading_period``/``resolve_custom_reading_range``/
``compute_reading_statistics``/``compute_vitals_summary`` -- pure aggregation
logic behind the reading-statistics and vitals-summary features, tested
independent of the API layer.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework import serializers

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.modules.pregnancy.vitals.models import VitalReading
from momcare_platform.modules.pregnancy.vitals.services import (
    READING_PERIODS,
    compute_reading_statistics,
    compute_vitals_summary,
    resolve_custom_reading_range,
    resolve_reading_period,
)

pytestmark = pytest.mark.django_db

UTC = ZoneInfo("UTC")


@pytest.fixture
def pregnancy_for(db):
    def _make(hospital, *, first_name="Ayesha", weeks_pregnant=28):
        lmp = timezone.now().date() - timedelta(weeks=weeks_pregnant)
        patient = onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": lmp},
        )
        return patient.current_pregnancy

    return _make


def _reading(pregnancy, *, recorded_at=None, **vitals):
    return VitalReading.objects.create(
        pregnancy=pregnancy,
        source=VitalReading.SOURCE_MANUAL,
        recorded_at=recorded_at or timezone.now(),
        **vitals,
    )


# ── resolve_reading_period ───────────────────────────────────────────────


@pytest.mark.parametrize(("code", "days"), list(READING_PERIODS.items()))
def test_resolve_reading_period_spans_the_expected_number_of_days(code, days):
    start, end = resolve_reading_period(code, UTC)

    assert (end.date() - start.date()).days == days - 1


def test_resolve_reading_period_rejects_an_unknown_code():
    with pytest.raises(serializers.ValidationError):
        resolve_reading_period("2_weeks", UTC)


# ── resolve_custom_reading_range ─────────────────────────────────────────


def test_resolve_custom_reading_range_is_inclusive_both_ends():
    start, end = resolve_custom_reading_range("2026-07-01", "2026-07-03", UTC)

    assert start.date().isoformat() == "2026-07-01"
    assert end.date().isoformat() == "2026-07-03"
    assert start.hour == 0
    assert end.hour == 23


def test_resolve_custom_reading_range_rejects_start_after_end():
    with pytest.raises(serializers.ValidationError):
        resolve_custom_reading_range("2026-07-10", "2026-07-01", UTC)


def test_resolve_custom_reading_range_rejects_a_malformed_date():
    with pytest.raises(serializers.ValidationError):
        resolve_custom_reading_range("not-a-date", "2026-07-01", UTC)


# ── compute_reading_statistics ───────────────────────────────────────────


def test_compute_reading_statistics_rejects_an_unknown_reading_type(make_hospital, pregnancy_for):
    hospital = make_hospital("Unknown Reading Type Hospital")
    pregnancy = pregnancy_for(hospital)

    with pytest.raises(serializers.ValidationError):
        compute_reading_statistics(pregnancy.readings.all(), "pulse_ox")


def test_blood_pressure_statistics_average_min_max_count(make_hospital, pregnancy_for):
    hospital = make_hospital("BP Statistics Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="150", diastolic_bp="100", heart_rate="110")
    _reading(pregnancy, systolic_bp="140", diastolic_bp="95", heart_rate="85")
    _reading(pregnancy, systolic_bp="142", diastolic_bp="93", heart_rate="78")

    stats = compute_reading_statistics(pregnancy.readings.all(), "blood_pressure")

    assert stats["average"] == {"systolic_bp": 144.0, "diastolic_bp": 96.0, "heart_rate": 91.0}
    assert stats["min"] == {"systolic_bp": 140.0, "diastolic_bp": 93.0, "heart_rate": 78.0}
    assert stats["max"] == {"systolic_bp": 150.0, "diastolic_bp": 100.0, "heart_rate": 110.0}
    assert stats["readings_count"] == {"systolic_bp": 3, "diastolic_bp": 3, "heart_rate": 3}


def test_blood_pressure_average_rounds_to_nearest_whole_number_not_nearest_ten(make_hospital, pregnancy_for):
    """150+140+141=431/3=143.66... -> nearest whole number is 144, returned as
    int, not float 143.67 and not bucketed to a nearest-ten value like 140."""
    hospital = make_hospital("BP Rounding Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="150", diastolic_bp="100", heart_rate="110")
    _reading(pregnancy, systolic_bp="140", diastolic_bp="95", heart_rate="85")
    _reading(pregnancy, systolic_bp="141", diastolic_bp="93", heart_rate="78")

    stats = compute_reading_statistics(pregnancy.readings.all(), "blood_pressure")

    assert stats["average"]["systolic_bp"] == 144
    assert isinstance(stats["average"]["systolic_bp"], int)


def test_temperature_average_keeps_one_decimal_as_a_float(make_hospital, pregnancy_for):
    hospital = make_hospital("Temperature Rounding Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, body_temp_f="98.4")
    _reading(pregnancy, body_temp_f="99.1")
    _reading(pregnancy, body_temp_f="99.9")

    stats = compute_reading_statistics(pregnancy.readings.all(), "temperature")

    assert stats["average"]["body_temp_f"] == 99.1
    assert isinstance(stats["average"]["body_temp_f"], float)


def test_blood_pressure_category_percentages(make_hospital, pregnancy_for):
    """3 stage-2 BP readings -> 100% Stage 2. 2 normal + 1 tachycardia heart
    rate -> 67%/33%, largest-remainder rounded, matching Neuro_RPM's own
    reference example."""
    hospital = make_hospital("BP Category Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="150", diastolic_bp="100", heart_rate="78")
    _reading(pregnancy, systolic_bp="140", diastolic_bp="95", heart_rate="85")
    _reading(pregnancy, systolic_bp="142", diastolic_bp="93", heart_rate="110")

    stats = compute_reading_statistics(pregnancy.readings.all(), "blood_pressure")

    bp_bands = {b["key"]: b for b in stats["categories"]["bp_category"]["bands"]}
    assert bp_bands["Stage 2"] == {"key": "Stage 2", "count": 3, "percentage": 100}
    assert bp_bands["Normal"]["count"] == 0

    hr_bands = {b["key"]: b for b in stats["categories"]["heart_rate_category"]["bands"]}
    assert hr_bands["Normal"] == {"key": "Normal", "count": 2, "percentage": 67}
    assert hr_bands["Tachycardia"] == {"key": "Tachycardia", "count": 1, "percentage": 33}
    assert sum(b["percentage"] for b in stats["categories"]["heart_rate_category"]["bands"]) == 100


def test_a_reading_missing_one_field_only_drops_out_of_that_fields_stats(make_hospital, pregnancy_for):
    hospital = make_hospital("Partial Reading Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="140", diastolic_bp="90", heart_rate="80")
    _reading(pregnancy, systolic_bp="142", diastolic_bp="92")  # no heart_rate

    stats = compute_reading_statistics(pregnancy.readings.all(), "blood_pressure")

    assert stats["readings_count"]["systolic_bp"] == 2
    assert stats["readings_count"]["heart_rate"] == 1


def test_single_field_reading_types(make_hospital, pregnancy_for):
    hospital = make_hospital("Single Field Types Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, body_temp_f="101.5")
    _reading(pregnancy, blood_glucose="130")
    _reading(pregnancy, hemoglobin="9")

    temp_stats = compute_reading_statistics(pregnancy.readings.all(), "temperature")
    assert temp_stats["average"] == {"body_temp_f": 101.5}
    assert temp_stats["categories"]["temperature_category"]["bands"][0]["key"] == "Hypothermia"

    glucose_stats = compute_reading_statistics(pregnancy.readings.all(), "blood_glucose")
    assert glucose_stats["average"] == {"blood_glucose": 130.0}
    diabetes_band = next(b for b in glucose_stats["categories"]["glucose_category"]["bands"] if b["key"] == "Diabetes")
    assert diabetes_band["count"] == 1

    hb_stats = compute_reading_statistics(pregnancy.readings.all(), "hemoglobin")
    assert hb_stats["average"] == {"hemoglobin": 9.0}
    moderate_band = next(
        b for b in hb_stats["categories"]["hemoglobin_category"]["bands"] if b["key"] == "Moderate Anemia"
    )
    assert moderate_band["count"] == 1


def test_wellness_has_no_categories_block(make_hospital, pregnancy_for):
    """stress_score/phys_activity_score have no defined clinical bands."""
    hospital = make_hospital("Wellness Statistics Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, stress_score="6", phys_activity_score="4")

    stats = compute_reading_statistics(pregnancy.readings.all(), "wellness")

    assert stats["average"] == {"stress_score": 6.0, "phys_activity_score": 4.0}
    assert stats["categories"] == {}


def test_empty_queryset_returns_all_empty_dicts(make_hospital, pregnancy_for):
    hospital = make_hospital("Empty Statistics Hospital")
    pregnancy = pregnancy_for(hospital)

    stats = compute_reading_statistics(pregnancy.readings.all(), "blood_pressure")

    assert stats == {"average": {}, "min": {}, "max": {}, "readings_count": {}, "categories": {}}


# ── compute_vitals_summary ───────────────────────────────────────────────


def test_vitals_summary_averages_the_last_30_days(make_hospital, pregnancy_for):
    hospital = make_hospital("Vitals Summary Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80", heart_rate="70")
    _reading(pregnancy, systolic_bp="130", diastolic_bp="90", heart_rate="80")

    summary = compute_vitals_summary(pregnancy)

    assert summary["last_30_days_average"]["systolic_bp"] == 125
    assert isinstance(summary["last_30_days_average"]["systolic_bp"], int)
    assert summary["last_30_days_average"]["diastolic_bp"] == 85
    assert summary["last_30_days_average"]["heart_rate"] == 75


def test_vitals_summary_rounds_a_fractional_average_to_the_nearest_whole_number(make_hospital, pregnancy_for):
    """120+130+141=391/3=130.33... -> 130, not truncated/bucketed."""
    hospital = make_hospital("Vitals Summary Rounding Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="120")
    _reading(pregnancy, systolic_bp="130")
    _reading(pregnancy, systolic_bp="141")

    summary = compute_vitals_summary(pregnancy)

    assert summary["last_30_days_average"]["systolic_bp"] == 130


def test_vitals_summary_excludes_readings_older_than_30_days(make_hospital, pregnancy_for):
    hospital = make_hospital("Vitals Summary Old Reading Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="200", diastolic_bp="150", recorded_at=timezone.now() - timedelta(days=45))
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80")

    summary = compute_vitals_summary(pregnancy)

    assert summary["last_30_days_average"]["systolic_bp"] == 120.0


def test_vitals_summary_is_null_not_zero_for_a_vital_with_no_readings(make_hospital, pregnancy_for):
    hospital = make_hospital("Vitals Summary No Data Hospital")
    pregnancy = pregnancy_for(hospital)
    _reading(pregnancy, systolic_bp="120", diastolic_bp="80")

    summary = compute_vitals_summary(pregnancy)

    assert summary["last_30_days_average"]["blood_glucose"] is None
    assert summary["last_30_days_average"]["hemoglobin"] is None
