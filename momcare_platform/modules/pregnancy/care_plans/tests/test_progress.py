"""The weekly progress summary: every number comes from the readings, none from a model."""

from datetime import UTC, datetime, timedelta

import pytest

from momcare_platform.modules.pregnancy.care_plans.progress import (
    BASED_ON_LAST_READING,
    BASED_ON_LAST_WEEK,
    BASED_ON_MONTH,
    TREND_IMPROVED,
    TREND_STEADY,
    TREND_WORSE,
    ReadingRow,
    averages,
    build_progress,
    concerns,
    mean_severity,
    risk_percentages,
    trend,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def row(risk="low", *, days=0, vitals=None, **values):
    return ReadingRow(recorded_at=T0 + timedelta(days=days), risk=risk, vitals=vitals or {}, values=values)


def rows(*risks, start=0):
    return [row(risk, days=start + i) for i, risk in enumerate(risks)]


# -- percentages ------------------------------------------------------------


def test_percentages_always_add_up_to_100():
    pct = risk_percentages(rows("low", "medium", "high"))
    assert sum(pct.values()) == 100
    assert sorted(pct.values()) == [33, 33, 34]


@pytest.mark.parametrize(
    "risks", [("low",), ("low", "high"), ("low",) * 7 + ("medium",) * 2 + ("high",), ("medium",) * 3]
)
def test_percentages_sum_to_100_for_any_mix(risks):
    assert sum(risk_percentages(rows(*risks)).values()) == 100


def test_percentages_of_nothing_is_empty():
    assert risk_percentages([]) == {}


def test_a_single_level_is_one_hundred_percent():
    assert risk_percentages(rows("high", "high")) == {"low": 0, "medium": 0, "high": 100}


# -- concerns / averages ------------------------------------------------------


def test_concerns_are_the_most_frequent_abnormal_categories():
    data = [
        row(vitals={"bp": "Stage 2", "hemoglobin": "Mild Anemia"}),
        row(vitals={"bp": "Stage 2", "hemoglobin": "Normal"}),
        row(vitals={"bp": "Normal", "hemoglobin": "Mild Anemia"}),
        row(vitals={"bp": "Stage 2", "hemoglobin": "Mild Anemia"}),
    ]
    found = concerns(data)
    assert found[0] == {"axis": "bp", "category": "Stage 2", "count": 3, "of": 4}
    assert found[1] == {"axis": "hemoglobin", "category": "Mild Anemia", "count": 3, "of": 4}


def test_normal_and_unrecorded_vitals_are_never_a_concern():
    assert concerns([row(vitals={"bp": "Normal", "glucose": ""})]) == []


def test_averages_skip_readings_without_that_number():
    data = [row(systolic_bp=140, hemoglobin=10.0), row(systolic_bp=130), row(hemoglobin=11.0)]
    assert averages(data) == {"systolic_bp": 135, "hemoglobin": 10.5}


# -- trend ------------------------------------------------------------------


def test_fewer_high_readings_is_improving_and_reports_the_low_risk_change():
    result = trend(recent=rows("low", "low", "medium"), earlier=rows("high", "high", "medium", "low"))
    assert result["direction"] == TREND_IMPROVED
    assert result["low_risk_points_change"] == result["recent_low_percent"] - result["earlier_low_percent"] > 0


def test_more_high_readings_is_worse():
    assert trend(rows("high", "high"), rows("low", "low", "medium"))["direction"] == TREND_WORSE


def test_a_small_difference_is_steady():
    same = rows("low", "low", "low", "medium")
    assert trend(same, rows("low", "low", "low", "medium"))["direction"] == TREND_STEADY


def test_the_mean_severity_orders_the_levels():
    assert mean_severity(rows("low")) < mean_severity(rows("medium")) < mean_severity(rows("high"))


# -- the summary ------------------------------------------------------------


def make(last_week, month_before, focus=("bp",), week=17, last_reading=None):
    return build_progress(
        week_number=week,
        last_week=last_week,
        month_before_week=month_before,
        focus_axes=list(focus),
        last_reading_at=last_reading,
    )


def test_a_week_with_readings_reports_last_weeks_conditions_and_the_month_trend():
    earlier = [row("high", days=i, vitals={"bp": "Stage 2"}) for i in range(4)]
    last = [row("low", days=10 + i, vitals={"bp": "Normal"}) for i in range(2)] + [
        row("medium", days=12, vitals={"bp": "Stage 2"})
    ]
    result = make(last, earlier + last)

    facts, text = result["facts"], result["text"]
    assert facts["based_on"] == BASED_ON_LAST_WEEK
    assert facts["last_week"]["readings"] == 3
    assert facts["trend"]["direction"] == TREND_IMPROVED
    assert "Last week (week 16) you had 3 readings: 67% low risk, 33% medium and 0% high." in text
    assert "blood pressure was Stage 2 in 1 of 3 readings" in text
    assert "your readings are improving: low-risk readings went from 0% to 67% (+67 points)" in text
    assert "Areas to work on this week: blood pressure." in text
    assert "week 17" in text


def test_a_getting_worse_month_says_so_with_a_minus_sign():
    earlier = rows("low", "low", "low", "low")
    last = rows("high", "high", start=10)
    text = make(last, earlier + last)["text"]
    assert "getting worse" in text
    assert "(−100 points)" in text


def test_a_week_with_no_readings_uses_the_whole_month_and_says_so():
    month = rows("high", "high", "medium", "low")
    result = make([], month)

    assert result["facts"]["based_on"] == BASED_ON_MONTH
    assert result["facts"]["last_week"] is None
    assert "There were no readings last week (week 16)" in result["text"]
    assert "4 readings" in result["text"]
    # four readings are enough to compare the later half of the month with the earlier half
    assert result["facts"]["trend"] is not None


def test_too_few_month_readings_means_no_trend_not_an_invented_one():
    result = make([], rows("high", "low"))
    assert result["facts"]["trend"] is None
    assert "improving" not in result["text"]
    assert "worse" not in result["text"]


def test_no_recent_readings_at_all_uses_the_last_reading_and_names_its_date():
    result = make([], [], last_reading=datetime(2026, 8, 20, tzinfo=UTC))
    assert result["facts"]["based_on"] == BASED_ON_LAST_READING
    assert "based on your last reading on 20 Aug 2026" in result["text"]


def test_the_first_week_of_readings_has_nothing_to_compare_with():
    last = rows("medium", "medium")
    result = make(last, last)
    assert result["facts"]["trend"] is None
    assert "nothing earlier to compare with" in result["text"]


def test_no_abnormal_vitals_means_nothing_to_work_on():
    result = make(rows("low", "low"), rows("low", "low"), focus=())
    assert "Keep doing what you are doing." in result["text"]
    assert "Areas to work on" not in result["text"]
    # the normal-range sentence appears once, not twice
    assert result["text"].count("outside the normal range") == 1


def test_the_text_is_built_only_from_the_facts():
    # The same facts always give the same words: nothing in the text is free-form.
    last = rows("low", "medium", "high")
    assert make(last, last)["text"] == make(last, last)["text"]


# -- wording that must not mislead ---------------------------------------------


def test_a_brand_new_patients_first_plan_says_it_is_her_first_plan_not_that_readings_are_missing():
    from datetime import date  # noqa: PLC0415

    result = build_progress(
        week_number=20,
        last_week=[],
        month_before_week=[],
        focus_axes=[],
        last_reading_at=datetime(2026, 9, 16, 8, 0, tzinfo=UTC),
        week_start=date(2026, 9, 16),
    )
    assert result["facts"]["first_plan"] is True
    assert "This is your first plan, based on your latest reading on 16 Sep 2026" in result["text"]
    assert "no readings from the last few weeks" not in result["text"]


def test_a_long_gap_still_says_there_were_no_recent_readings():
    from datetime import date  # noqa: PLC0415

    result = build_progress(
        week_number=30,
        last_week=[],
        month_before_week=[],
        focus_axes=[],
        last_reading_at=datetime(2026, 8, 1, tzinfo=UTC),
        week_start=date(2026, 9, 16),
    )
    assert result["facts"]["first_plan"] is False
    assert "no readings from the last few weeks" in result["text"]


def test_a_bad_week_is_never_told_to_keep_doing_what_she_is_doing():
    last = rows("high", "high", "medium", "low")
    text = make(last, last, focus=())["text"]
    assert "Keep doing what you are doing." not in text
    assert "not all in the healthy range" in text


def test_a_worsening_trend_is_not_told_to_keep_doing_what_she_is_doing():
    earlier = rows("low", "low", "low", "low")
    last = rows("low", "low", "low", "low", "low", "high", "high", "high", "high", "high", start=10)
    text = make(last, earlier + last, focus=())["text"]
    assert "Keep doing what you are doing." not in text


def test_a_week_at_a_month_boundary_is_compared_with_the_week_before():
    last = rows("high", "high", "medium", "high")
    before = rows("low", "low", "low", "low", start=-10)
    result = build_progress(
        week_number=22,
        last_week=last,
        month_before_week=last,  # the month began inside last week: nothing earlier in it
        focus_axes=["bp"],
        prior_week=before,
    )
    assert result["facts"]["trend"]["direction"] == TREND_WORSE
    assert result["facts"]["trend"]["against"] == "week_before"
    assert "Compared with the week before your readings have been getting worse" in result["text"]
    assert "nothing earlier to compare with" not in result["text"]


# -- progress for nutrition and for exercise, each on its own vitals --------------


def vrow(risk="low", days=0, **vitals):
    return row(risk, days=days, vitals=vitals)


def week_rows():
    # 4 readings: hemoglobin low in 2 of them, heart rate high in 1, blood pressure always normal
    return [
        vrow("medium", 10, hemoglobin="Mild Anemia", heart_rate="Normal", bp="Normal"),
        vrow("medium", 11, hemoglobin="Mild Anemia", heart_rate="Tachycardia", bp="Normal"),
        vrow("low", 12, hemoglobin="Normal", heart_rate="Normal", bp="Normal"),
        vrow("low", 13, hemoglobin="Normal", heart_rate="Normal", bp="Normal"),
    ]


def test_each_section_reports_only_its_own_vitals():
    result = build_progress(
        week_number=17, last_week=week_rows(), month_before_week=week_rows(), focus_axes=["hemoglobin"]
    )
    nutrition, exercise = result["nutrition"], result["exercise"]

    assert nutrition["facts"]["flagged_readings"] == 2  # the two low-hemoglobin readings
    assert exercise["facts"]["flagged_readings"] == 1  # the one fast heart rate
    assert "hemoglobin was Mild Anemia in 2 of 4" in nutrition["text"]
    assert "heart rate" not in nutrition["text"]
    assert "hemoglobin" not in exercise["text"]
    assert "heart rate was Tachycardia in 1 of 4" in exercise["text"]


def test_a_vital_belonging_to_neither_section_is_in_neither():
    rows_ = [vrow("medium", i, temperature="Fever") for i in range(3)]
    result = build_progress(week_number=17, last_week=rows_, month_before_week=rows_, focus_axes=["temperature"])
    assert result["nutrition"]["facts"]["flagged_readings"] == 0
    assert result["exercise"]["facts"]["flagged_readings"] == 0
    assert "temperature" not in result["nutrition"]["text"] + result["exercise"]["text"]


def test_blood_pressure_belongs_to_both_sections():
    rows_ = [vrow("medium", i, bp="Stage 2") for i in range(2)]
    result = build_progress(week_number=17, last_week=rows_, month_before_week=rows_, focus_axes=["bp"])
    assert "blood pressure was Stage 2 in 2 of 2" in result["nutrition"]["text"]
    assert "blood pressure was Stage 2 in 2 of 2" in result["exercise"]["text"]


def test_a_section_trend_compares_the_share_of_flagged_readings():
    earlier = [vrow("low", i, hemoglobin="Normal") for i in range(4)]
    last = [vrow("medium", 10 + i, hemoglobin="Mild Anemia") for i in range(4)]
    result = build_progress(
        week_number=17, last_week=last, month_before_week=earlier + last, focus_axes=["hemoglobin"]
    )

    trend_ = result["nutrition"]["facts"]["trend"]
    assert (trend_["earlier_percent"], trend_["recent_percent"], trend_["points_change"]) == (0, 100, 100)
    assert trend_["direction"] == TREND_WORSE
    assert "is getting worse: 0% before, 100% now" in result["nutrition"]["text"]
    # nothing about exercise's vitals moved, so exercise is steady
    assert result["exercise"]["facts"]["trend"]["direction"] == TREND_STEADY


def test_a_section_with_nothing_abnormal_says_so_and_keeps_going():
    good = [vrow("low", i, hemoglobin="Normal", glucose="Normal", bp="Normal") for i in range(3)]
    result = build_progress(week_number=17, last_week=good, month_before_week=good, focus_axes=[])
    assert "none of your diet-related measures" in result["nutrition"]["text"]
    assert result["nutrition"]["text"].endswith("Keep doing what you are doing.")


def test_a_bad_week_in_a_section_is_never_told_to_keep_doing_what_she_is_doing():
    bad = [vrow("medium", i, hemoglobin="Mild Anemia") for i in range(3)]
    text = build_progress(week_number=17, last_week=bad, month_before_week=bad, focus_axes=[])["nutrition"]["text"]
    assert "Keep doing what you are doing." not in text
    assert "Take extra care" in text


def test_a_section_summary_for_a_first_plan_is_built_from_the_latest_reading():
    from datetime import date  # noqa: PLC0415

    result = build_progress(
        week_number=20,
        last_week=[],
        month_before_week=[],
        focus_axes=["hemoglobin", "heart_rate"],
        last_reading_at=datetime(2026, 9, 16, 8, tzinfo=UTC),
        week_start=date(2026, 9, 16),
    )
    assert result["nutrition"]["text"] == "Your latest reading showed: hemoglobin."
    assert result["exercise"]["text"] == "Your latest reading showed: heart rate."
