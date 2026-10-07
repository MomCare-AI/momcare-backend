"""What a reading does to a patient's weekly care plan: the first reading of a week writes
the week's plan, a worse reading gets quick advice, only a worse state that persists
re-plans the week, and a failing model never fails the reading.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from momcare_platform.core.ai.openrouter_client import Researched
from momcare_platform.core.patients.models import Pregnancy
from momcare_platform.modules.pregnancy.care_plans import services
from momcare_platform.modules.pregnancy.care_plans.guardrails import AI_ONLY_SOURCE, FALLBACK_SOURCE
from momcare_platform.modules.pregnancy.care_plans.models import CarePlan, CarePlanSectionVersion, CareWeek
from momcare_platform.modules.pregnancy.care_plans.sweep import run_sweep
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import (
    HIGH,
    LOW,
    MEDIUM,
    SOURCE_NAME,
    advice_json,
    all_advice,
    answer_for,
    current_plan,
    current_week,
    plan_json,
    section_of,
)
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment

pytestmark = pytest.mark.django_db


def versions(plan):
    return CarePlanSectionVersion.objects.filter(care_plan=plan)


def test_the_first_reading_of_a_week_writes_that_weeks_plan_with_both_sections(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("First Reading Hospital"), weeks=20)  # day 140 -> month 5, week 20

    add_reading(patient, MEDIUM)

    plan, week = current_plan(patient), current_week(patient)
    assert plan.month_number == 5
    assert week.week_number == 20
    assert week.week_start == timezone.localdate()  # 20w0d: the week starts today
    assert week.week_end == week.week_start + timedelta(days=6)
    assert plan.status == CarePlan.STATUS_IN_PROGRESS
    assert set(versions(plan).values_list("section", flat=True)) == {"nutrition", "exercise"}
    assert all(v.week_id == week.id for v in versions(plan))
    assert fake_model.count == 1
    assert fake_model.advice_count == 0  # the plan was just written for this very state
    assert not versions(plan).filter(is_fallback=True).exists()


def test_the_plan_period_starts_on_day_one_of_the_pregnancy(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Period Hospital"), weeks=20)
    add_reading(patient, MEDIUM)

    plan = current_plan(patient)
    day_one = patient.current_pregnancy.edd - timedelta(days=280)
    assert plan.period_start == day_one + timedelta(days=120)
    assert plan.period_end == plan.period_start + timedelta(days=29)


def test_an_unchanged_patient_keeps_the_weeks_plan_without_calling_the_model_again(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Unchanged Hospital"))
    add_reading(patient, MEDIUM)
    add_reading(patient, MEDIUM)

    assert fake_model.count == 1
    assert fake_model.advice_count == 0
    assert versions(current_plan(patient)).count() == 2  # one per section, not four


def test_the_weeks_plan_comes_with_a_progress_summary_the_model_is_given(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Progress Hospital"))

    add_reading(patient, MEDIUM)

    week = current_week(patient)
    assert week.progress["facts"]["based_on"] == "last_reading"
    assert week.progress["facts"]["week_number"] == 20
    assert "Areas to work on this week" in week.progress["text"]
    # Each section is told how she is doing in ITS OWN vitals, not the overall summary.
    for section in ("nutrition", "exercise"):
        prompt = fake_model.calls_for(section)[0]
        assert "HOW SHE HAS BEEN DOING" in prompt
        assert week.progress[section]["text"] in prompt
    assert week.progress["text"] not in fake_model.calls_for("nutrition")[0]


def test_a_worse_reading_mid_week_gives_quick_advice_and_leaves_the_weekly_plan_alone(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Quick Advice Hospital"))
    add_reading(patient, LOW)

    add_reading(patient, HIGH)

    assert fake_model.count == 1  # the weekly plan was not rewritten
    assert versions(current_plan(patient)).count() == 2
    advice = all_advice(patient).get()
    assert advice.risk_level == "high"
    assert advice.content["tips"]
    assert advice.content["contact_care_team"] is True  # at high risk, set by code
    assert advice.content["sources"] == [SOURCE_NAME]  # the weekly plan's own sources
    assert advice.content["basis"] == "web_search"
    assert advice.is_fallback is False
    assert current_week(patient).worse_readings == 1


def test_a_better_reading_changes_nothing_loosening_waits_for_the_next_weekly_plan(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Better Hospital"))
    add_reading(patient, HIGH)

    add_reading(patient, LOW)

    plan = current_plan(patient)
    assert fake_model.count == 1
    assert plan.current_state["risk"] == "high"  # still the strict plan
    assert fake_model.advice_count == 0  # low risk needs no quick advice


def test_a_better_but_still_medium_reading_gets_advice_not_a_new_plan(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Medium After High Hospital"))
    add_reading(patient, HIGH)

    add_reading(patient, MEDIUM)
    add_reading(patient, MEDIUM)

    assert fake_model.count == 1
    assert all_advice(patient).count() == 1  # the second identical reading adds nothing
    assert all_advice(patient).get().risk_level == "medium"


def test_the_same_worse_condition_again_gets_no_second_advice_and_the_third_in_a_row_replans(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Same Worse Hospital"))
    add_reading(patient, LOW)

    add_reading(patient, HIGH)  # worse (1 of 3): quick advice
    add_reading(patient, HIGH)  # worse (2 of 3): the same condition, so no second advice
    assert fake_model.advice_count == 1
    assert fake_model.count == 1  # and the week's plan is still the original

    add_reading(patient, HIGH)  # worse (3 in a row): the plan changes

    assert fake_model.count == 2
    assert current_week(patient).replans == 1


def test_a_medium_reading_the_plan_was_written_for_needs_no_quick_advice(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Medium Baseline Hospital"))
    add_reading(patient, MEDIUM)
    add_reading(patient, MEDIUM)
    assert fake_model.advice_count == 0


def test_a_worse_condition_that_persists_replans_the_week(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Persistent Hospital"))
    add_reading(patient, LOW)  # the week's plan is written for this
    add_reading(patient, HIGH)  # morning: quick advice only
    add_reading(patient, HIGH)  # afternoon: still worse
    assert fake_model.count == 1
    assert all_advice(patient).count() == 1
    progress_before = current_week(patient).progress

    add_reading(patient, HIGH)  # evening: three worse readings in a row, so the plan changes

    week = current_week(patient)
    assert fake_model.count == 2
    assert week.replans == 1
    assert "stayed worse" in week.last_replan_reason
    assert week.baseline_state["risk"] == "high"
    assert (week.worse_since, week.worse_readings) == (None, 0)
    assert current_plan(patient).current_state["risk"] == "high"
    assert all_advice(patient).count() == 1  # the re-plan itself is the response
    assert week.progress == progress_before  # the week's summary is kept, not rebuilt


def test_there_is_no_time_rule_three_worse_readings_a_minute_apart_replan(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Count Only Hospital"))
    add_reading(patient, LOW, minutes_ago=6)
    add_reading(patient, HIGH, minutes_ago=3)
    add_reading(patient, HIGH, minutes_ago=2)
    assert fake_model.count == 1  # two worse readings are not enough, however close together

    add_reading(patient, HIGH, minutes_ago=1)

    assert fake_model.count == 2
    assert current_week(patient).replans == 1


def test_one_worse_reading_never_replans_however_long_ago_the_plan_was_written(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("One Worse Hospital"))
    add_reading(patient, LOW, minutes_ago=3000)
    add_reading(patient, HIGH, minutes_ago=0)

    assert fake_model.count == 1
    assert fake_model.advice_count == 1


def test_a_reading_that_is_no_longer_worse_resets_the_persistence_count(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Reset Persistence Hospital"))
    add_reading(patient, LOW)
    add_reading(patient, HIGH)  # worse: the run starts
    add_reading(patient, LOW)  # back to what the plan expected: the run ends
    add_reading(patient, HIGH)  # worse again: a new run, only one reading long

    assert fake_model.count == 1
    week = current_week(patient)
    assert week.worse_readings == 1
    assert week.replans == 0


def test_a_change_in_allergies_replans_the_week_at_once(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Structural Hospital"))
    add_reading(patient, LOW)
    patient.food_allergies = ["oats"]
    patient.save(update_fields=["food_allergies"])

    add_reading(patient, LOW)

    assert fake_model.count == 2
    assert current_week(patient).replans == 1
    assert "allergies" in current_week(patient).last_replan_reason


def test_a_new_pregnancy_week_writes_a_new_plan_from_last_weeks_readings(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("New Week Hospital"), weeks=20)  # week 20 starts today
    add_reading(patient, MEDIUM)

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=7)):
        add_reading(patient, MEDIUM)

    week = current_week(patient)
    assert week.week_number == 21
    assert CareWeek.objects.filter(care_plan__pregnancy=patient.current_pregnancy).count() == 2
    assert fake_model.count == 2
    assert week.progress["facts"]["based_on"] == "last_week"
    # both of today's readings fall inside week 20, the week before the one being planned
    assert "Last week (week 20) you had 2 readings" in week.progress["text"]
    assert "Last week (week 20)" in fake_model.calls_for("nutrition")[-1]
    assert "Last week (week 20)" in fake_model.calls_for("exercise")[-1]


def test_a_week_with_no_readings_is_planned_from_the_months_readings(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Quiet Week Hospital"), weeks=18)  # day 126, month 5 (days 120-149)
    add_reading(patient, MEDIUM)

    # Two weeks on, nobody has sent a reading in the week before: week 20, still month 5.
    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=14)):
        assert services.start_week_if_needed(patient.current_pregnancy) is True

    week = current_week(patient)
    assert week.week_number == 20
    assert week.progress["facts"]["based_on"] == "month"
    assert week.progress["facts"]["last_week"] is None
    assert "There were no readings last week (week 19)" in week.progress["text"]
    assert fake_model.count == 2


def test_a_pregnancy_with_no_readings_at_all_gets_no_plan(make_hospital, make_patient, fake_model):
    patient = make_patient(make_hospital("No Readings Hospital"))

    assert services.start_week_if_needed(patient.current_pregnancy) is False
    assert not CareWeek.objects.exists()
    assert fake_model.count == 0


def test_a_model_failure_on_advice_stores_the_safe_generic_advice(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Advice Fallback Hospital"))
    add_reading(patient, LOW)
    fake_model.reply_for["advice"] = None  # the model is down for this request

    add_reading(patient, HIGH)

    advice = all_advice(patient).get()
    assert advice.is_fallback is True
    assert advice.content["contact_care_team"] is True
    assert advice.content["sources"][0].startswith("MomCare generic safe baseline")


@pytest.mark.parametrize(
    "bad",
    [
        advice_json(tips=["Take an iron tablet with lunch."]),
        "not json",
        advice_json(tips=["Go for a long jog to clear your head."]),  # nothing strenuous at high risk
    ],
)
def test_invalid_advice_is_replaced_by_the_safe_generic_advice(
    make_hospital, make_patient, add_reading, fake_model, bad
):
    patient = make_patient(make_hospital("Invalid Advice Hospital"))
    add_reading(patient, LOW)
    fake_model.reply_for["advice"] = bad

    add_reading(patient, HIGH)

    assert all_advice(patient).get().is_fallback is True


def test_high_risk_advice_always_says_to_contact_the_care_team_even_if_the_model_does_not(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Contact Hospital"))
    add_reading(patient, LOW)
    fake_model.reply_for["advice"] = advice_json(contact=False)

    add_reading(patient, HIGH)

    assert all_advice(patient).get().content["contact_care_team"] is True


def test_advice_names_nothing_the_patient_is_allergic_to(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Advice Allergy Hospital"), food_allergies=["yogurt"])
    add_reading(patient, LOW)
    fake_model.reply_for["advice"] = advice_json(
        tips=["Have some yogurt to settle your stomach.", "Rest and drink water."]
    )

    add_reading(patient, HIGH)

    tips = all_advice(patient).get().content["tips"]
    assert tips == ["Rest and drink water."]


def test_the_advice_prompt_looks_at_the_weekly_plan_and_is_short(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Advice Prompt Hospital"))
    add_reading(patient, LOW)

    add_reading(patient, HIGH)

    prompt = fake_model.calls_for("advice")[0]
    assert "THE WEEKLY PLAN SHE IS FOLLOWING" in prompt
    assert "Foods to avoid this week: Raw eggs" in prompt
    assert "Activities this week: A gentle walk" in prompt
    assert "At most 100 words" in prompt
    assert "Stay consistent with the weekly plan" in prompt
    assert "Zainab" not in prompt


# -- failing safely ----------------------------------------------------------


def test_a_model_outage_stores_the_safe_baseline_and_still_saves_the_reading(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.reply = None  # openrouter_client.generate() returns None on any transport failure
    patient = make_patient(make_hospital("Outage Hospital"))

    reading = add_reading(patient, HIGH)

    assert reading.pk is not None
    plan = current_plan(patient)
    assert versions(plan).filter(is_fallback=True).count() == 2
    # Each section tries once WITH the web search, once without (in case the search was
    # what failed), then stops: a dead service is not retried further.
    assert sorted(fake_model.web_flags) == [False, False, True, True]


def test_unusable_output_is_retried_then_written_once_more_without_the_search(
    make_hospital, make_patient, add_reading, fake_model
):
    """Answers that never parse: two tries with the web search, a third without it, and
    only then the generic baseline."""
    fake_model.reply = "not json"
    patient = make_patient(make_hospital("Invalid Output Hospital"))

    add_reading(patient, MEDIUM)

    plan = current_plan(patient)
    assert fake_model.count == 3
    assert versions(plan).filter(is_fallback=True).count() == 2  # both sections end on the baseline
    assert fake_model.web_flags.count(True) == 4 and fake_model.web_flags.count(False) == 2


def test_a_model_that_only_parses_without_the_search_still_gets_its_own_plan_labelled_ai(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.prose_when_searching = True
    patient = make_patient(make_hospital("Plain Works Hospital"))

    add_reading(patient, MEDIUM)

    for section in ("nutrition", "exercise"):
        version = versions(current_plan(patient)).get(section=section)
        assert version.is_fallback is False
        assert version.content["basis"] == "ai_only"


@pytest.mark.parametrize("bad_tip", [" ".join(["word"] * 500)])
def test_a_section_that_fails_validation_falls_back_alone_the_other_is_untouched(
    make_hospital, make_patient, add_reading, fake_model, bad_tip
):
    fake_model.reply_for["nutrition"] = plan_json(extra_tip=bad_tip)
    patient = make_patient(make_hospital("One Section Hospital"))

    add_reading(patient, MEDIUM)

    plan = current_plan(patient)
    nutrition = versions(plan).get(section="nutrition")
    exercise = versions(plan).get(section="exercise")
    assert nutrition.is_fallback is True
    assert exercise.is_fallback is False
    assert len(fake_model.calls_for("nutrition")) == 3  # twice with the search, once without, then the baseline
    assert len(fake_model.calls_for("exercise")) == 1


def test_a_retry_that_succeeds_is_used(make_hospital, make_patient, add_reading, fake_model):
    calls = {"nutrition": 0}

    def reply(prompt):
        if section_of(prompt) == "nutrition":
            calls["nutrition"] += 1
            if calls["nutrition"] == 1:
                return "garbage"
        return plan_json()

    fake_model.reply = reply
    patient = make_patient(make_hospital("Retry Hospital"))

    add_reading(patient, MEDIUM)

    assert not versions(current_plan(patient)).filter(is_fallback=True).exists()
    assert calls["nutrition"] == 2


def test_a_crash_inside_generation_never_fails_the_reading(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Crash Hospital"))
    with patch.object(services, "run_section_request", side_effect=RuntimeError("boom")):
        reading = add_reading(patient, MEDIUM)

    assert reading.pk is not None
    assert RiskAssessment.objects.filter(pregnancy=patient.current_pregnancy).exists()  # scoring survived
    plan = current_plan(patient)
    assert plan is None or not versions(plan).exists()


# -- guardrails applied to real generated plans ------------------------------


def test_a_food_the_patient_is_allergic_to_never_reaches_her_plan(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Allergy Hospital"), food_allergies=["oats"])

    add_reading(patient, MEDIUM)

    nutrition = versions(current_plan(patient)).get(section="nutrition")
    texts = [m["text"] for m in nutrition.content["meals"]]
    assert "Oats with milk" not in texts
    assert "Lentils with roti" in texts
    assert "oats" in nutrition.inputs["allergies"]
    assert "Oats with milk" in nutrition.inputs["removed_for_allergy"]


def test_a_high_risk_patient_is_never_given_more_than_the_gentle_baseline(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.reply = plan_json(
        activities=[
            {
                "item_key": "jogging",
                "text": "Jog",
                "duration_minutes": 60,
                "frequency_per_week": 6,
                "intensity": "vigorous",
            },
            {
                "item_key": "walking",
                "text": "Walk",
                "duration_minutes": 50,
                "frequency_per_week": 5,
                "intensity": "moderate",
            },
        ]
    )
    patient = make_patient(make_hospital("Cap Hospital"))

    add_reading(patient, HIGH)

    exercise = versions(current_plan(patient)).get(section="exercise")
    assert [a["item_key"] for a in exercise.content["activities"]] == ["walking"]
    assert exercise.content["activities"][0]["intensity"] == "light"
    assert exercise.content["activities"][0]["duration_minutes"] <= 15


def test_the_prompt_never_contains_who_the_patient_is(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(
        make_hospital("Privacy Hospital"), first_name="Zainab", national_id="35202-1234567-1", phone="03001234567"
    )

    add_reading(patient, MEDIUM)

    prompt = fake_model.calls_for("nutrition")[0]
    for private in ("Zainab", "Bibi", "35202-1234567-1", "03001234567", str(patient.id), patient.mrn or "~none~"):
        assert private not in prompt
    assert "Asia" in prompt  # the region is sent; the person is not


# -- months ------------------------------------------------------------------


def test_a_new_month_starts_a_new_monthly_plan_and_a_new_week(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Rollover Hospital"), weeks=20)
    add_reading(patient, MEDIUM)
    first = current_plan(patient)

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=35)):
        add_reading(patient, MEDIUM)

    second = current_plan(patient)
    assert second.pk != first.pk
    assert second.month_number == first.month_number + 1
    assert fake_model.count == 2
    assert versions(second).count() == 2


def test_a_new_month_with_a_changed_patient_generates_a_fresh_plan(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Rollover Change Hospital"), weeks=20)
    add_reading(patient, MEDIUM)

    in_a_month = timezone.localdate() + timedelta(days=30)
    with patch("django.utils.timezone.localdate", return_value=in_a_month):
        add_reading(patient, HIGH)

    assert fake_model.count == 2


def test_without_a_due_date_there_is_no_plan_month():
    assert services.ensure_care_plan(Pregnancy(edd=None)) is None


def test_a_deactivated_patient_gets_no_plan(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Inactive Hospital"))
    patient.is_active = False
    patient.save(update_fields=["is_active"])

    add_reading(patient, MEDIUM)

    assert current_plan(patient) is None
    assert fake_model.count == 0


def test_the_model_used_is_the_platform_wide_ai_setting(make_hospital, make_patient, add_reading):
    from momcare_platform.core.ai.services import get_ai_config  # noqa: PLC0415

    config = get_ai_config()
    config.current_model = "example/some-model"
    config.save(update_fields=["current_model"])
    seen = {}

    def spy(prompt, *, model, max_tokens, timeout=10.0, web_search=True):
        if section_of(prompt) is not None:
            seen["model"] = model
            return Researched(answer_for(prompt, plan_json()) or "", [])
        return None

    patient = make_patient(make_hospital("Config Hospital"))
    with patch("momcare_platform.core.ai.openrouter_client.generate_researched", new=spy):
        add_reading(patient, MEDIUM)

    assert seen["model"] == "example/some-model"
    assert versions(current_plan(patient)).first().model_name == "example/some-model"


def test_low_risk_patients_get_a_plan_too(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Low Risk Hospital"))
    add_reading(patient, LOW)
    assert versions(current_plan(patient)).count() == 2


# -- what the model is told ---------------------------------------------------


def test_the_prompt_asks_for_a_short_plan_and_names_the_word_limits(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Short Prompt Hospital"))
    add_reading(patient, MEDIUM)

    nutrition = fake_model.calls_for("nutrition")[0]
    exercise = fake_model.calls_for("exercise")[0]
    assert "at most 300 words" in nutrition
    assert "at most 120 words" in exercise
    for prompt in (nutrition, exercise):
        assert "7-day" not in prompt
        assert "weekly_rotation" not in prompt


def test_the_prompt_orders_a_web_search_for_the_patients_countrys_official_guidance_first(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Sources Prompt Hospital"))  # the fixture hospital is in Pakistan
    add_reading(patient, MEDIUM)

    for section, topic in (("nutrition", "nutrition and diet"), ("exercise", "physical activity and exercise")):
        prompt = fake_model.calls_for(section)[0]
        assert "RESEARCH FIRST (mandatory)" in prompt
        assert f"SEARCH THE WEB for the official {topic} guidance" in prompt
        assert "health ministry or national health authority of Pakistan" in prompt
        assert prompt.index("pages from Pakistan itself") < prompt.index("WHO guidance only to fill")
        assert "Ignore pages about other countries" in prompt
        assert "Never draw on social media" in prompt
        assert "Do not pretend a document exists" in prompt
        assert "Do NOT write web links" in prompt


def test_every_plan_request_turns_the_web_search_on(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Search On Hospital"))
    add_reading(patient, MEDIUM)

    assert fake_model.web_flags == [True, True]  # nutrition and exercise (both searched)


# -- two separate requests, each with its own sources and timestamp -----------


def test_nutrition_and_exercise_are_two_separate_requests_with_their_own_prompts(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Two Requests Hospital"))

    add_reading(patient, MEDIUM)

    assert len(fake_model.calls_for("nutrition")) == 1
    assert len(fake_model.calls_for("exercise")) == 1
    nutrition, exercise = fake_model.calls_for("nutrition")[0], fake_model.calls_for("exercise")[0]
    assert "PHYSICAL-ACTIVITY" not in nutrition
    assert "NUTRITION plan" not in exercise
    assert '"foods_to_eat"' in nutrition
    assert '"activities"' not in nutrition
    assert '"activities"' in exercise
    assert '"foods_to_eat"' not in exercise


def test_each_section_stores_the_pages_the_search_found_and_its_creation_time(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Sources Stored Hospital"))

    add_reading(patient, MEDIUM)

    plan = current_plan(patient)
    for section in ("nutrition", "exercise"):
        version = versions(plan).get(section=section)
        assert version.content["basis"] == "web_search"
        assert version.content["sources"] == [SOURCE_NAME]
        assert version.content["source_links"][0]["url"] == fake_model.citations[0]["url"]
        assert version.created_at is not None
        assert version.is_fallback is False


def test_when_the_search_finds_no_official_page_the_plan_is_kept_and_labelled_as_generated_by_ai(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.citations = []
    patient = make_patient(make_hospital("No Source Hospital"))

    add_reading(patient, MEDIUM)

    for section in ("nutrition", "exercise"):
        version = versions(current_plan(patient)).get(section=section)
        assert version.is_fallback is False  # the AI's own plan is shown, not the generic baseline
        assert version.content["basis"] == "ai_only"
        assert version.content["sources"] == [AI_ONLY_SOURCE]
        assert version.content["source_links"] == []


def test_if_the_search_itself_is_down_the_plan_is_written_without_it_and_says_generated_by_ai(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.search_down = True
    patient = make_patient(make_hospital("Search Down Hospital"))

    add_reading(patient, MEDIUM)

    assert sorted(fake_model.web_flags) == [False, False, True, True]  # each section: the search, then plain
    for section in ("nutrition", "exercise"):
        version = versions(current_plan(patient)).get(section=section)
        assert version.is_fallback is False
        assert version.content["basis"] == "ai_only"
        assert version.content["sources"] == [AI_ONLY_SOURCE]


def test_the_models_own_claimed_sources_and_links_are_never_stored(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.reply = plan_json(sources=["https://example.com/some-blog", "An invented 2031 WHO guideline"])
    patient = make_patient(make_hospital("Link Hospital"))

    add_reading(patient, MEDIUM)

    nutrition = versions(current_plan(patient)).get(section="nutrition")
    assert nutrition.content["sources"] == [SOURCE_NAME]
    assert "example.com" not in str(nutrition.content)
    assert "invented" not in str(nutrition.content)


def test_the_fallback_plan_names_momcares_own_baseline_as_its_source(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.reply = None
    patient = make_patient(make_hospital("Fallback Source Hospital"))

    add_reading(patient, MEDIUM)

    for section in ("nutrition", "exercise"):
        assert versions(current_plan(patient)).get(section=section).content["sources"] == [FALLBACK_SOURCE]


def test_the_prompts_name_the_hospitals_country_so_foods_fit_where_she_lives(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Country Prompt Hospital"))  # the fixture hospital is in Pakistan

    add_reading(patient, MEDIUM)

    for section in ("nutrition", "exercise"):
        prompt = fake_model.calls_for(section)[0]
        assert "Pakistan (Asia)" in prompt
        assert "religious and cultural food norms" in prompt


def test_a_week_that_straddles_a_month_edge_stays_in_the_month_holding_its_first_day(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Straddle Hospital"), weeks=21)  # week 21 = days 147-153, month 5 = 120-149

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=3)):
        add_reading(patient, MEDIUM)  # day 150: already month 6 by the calendar of months

    plan, week = current_plan(patient), current_week(patient)
    assert week.week_number == 21
    assert plan.month_number == 5  # the month holding the week's first day
    assert CarePlan.objects.filter(pregnancy=patient.current_pregnancy).count() == 1


# -- the new week starts at 12 am where she lives -----------------------------


def test_a_pregnancy_week_starts_at_midnight_in_the_hospitals_time_zone(make_hospital, fake_model):
    from datetime import UTC, date, datetime  # noqa: PLC0415

    from momcare_platform.core.patients.services import onboard_patient  # noqa: PLC0415

    hospital = make_hospital("Midnight Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Midnight", "last_name": "Patient"},
        pregnancy_data={"lmp": date(2026, 5, 19)},  # week 20 starts 6 Oct, week 21 on 13 Oct
    )
    patient.location.timezone = "Asia/Karachi"  # UTC+5
    patient.location.save()
    pregnancy = patient.current_pregnancy

    # 23:30 in Karachi on 12 Oct (18:30 UTC): still week 20
    with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 12, 18, 30, tzinfo=UTC)):
        found = services.ensure_week(pregnancy)
        assert found is not None
        assert found[1].week_number == 20

    # 00:30 in Karachi on 13 Oct (19:30 UTC on the 12th): week 21 has begun
    with patch("django.utils.timezone.now", return_value=datetime(2026, 10, 12, 19, 30, tzinfo=UTC)):
        found = services.ensure_week(pregnancy)
        assert found is not None
        assert found[1].week_number == 21


def test_the_sweep_writes_the_new_weeks_plan_after_midnight_without_waiting_for_a_reading(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Sweep Midnight Hospital"), weeks=20)
    add_reading(patient, MEDIUM)
    assert CareWeek.objects.count() == 1

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=7)):
        started, retried = run_sweep()

    assert (started, retried) == (1, 0)
    assert CareWeek.objects.count() == 2
    assert fake_model.count == 2


def test_a_week_whose_creation_failed_is_simply_created_again_by_the_next_sweep(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Retry Creation Hospital"), weeks=20)
    add_reading(patient, MEDIUM)
    seven_days_on = timezone.localdate() + timedelta(days=7)

    with patch("django.utils.timezone.localdate", return_value=seven_days_on):
        with patch.object(services, "run_section_request", side_effect=RuntimeError("down")):
            assert run_sweep() == (0, 0)  # the failure is contained: the sweep itself does not crash
        assert CareWeek.objects.filter(week_number=21).count() == 0  # and nothing half-written is left behind
        assert run_sweep() == (1, 0)  # the next run creates it

    assert CareWeek.objects.filter(week_number=21).count() == 1


def test_one_patients_failure_does_not_stop_everyone_elses_week_from_starting(
    make_hospital, make_patient, add_reading, fake_model
):
    hospital = make_hospital("Contained Failure Hospital")
    first = make_patient(hospital, first_name="First", weeks=20)
    second = make_patient(hospital, first_name="Second", weeks=20)
    add_reading(first, MEDIUM)
    add_reading(second, MEDIUM)
    real = services.start_week_if_needed

    def flaky(pregnancy):
        if pregnancy.pk == first.current_pregnancy.pk:
            raise RuntimeError("this one fails")
        return real(pregnancy)

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=7)):
        with patch("momcare_platform.modules.pregnancy.care_plans.sweep.start_week_if_needed", side_effect=flaky):
            assert run_sweep() == (1, 0)

    assert CareWeek.objects.filter(care_plan__pregnancy=second.current_pregnancy, week_number=21).exists()
    assert not CareWeek.objects.filter(care_plan__pregnancy=first.current_pregnancy, week_number=21).exists()


# -- banned wording from an official document: the entry goes, the plan stays ---


@pytest.mark.parametrize("bad_tip", ["Take an iron tablet daily", "Aim for 2000 calories", "Ask about folic acid"])
def test_an_entry_with_banned_wording_is_dropped_but_the_rest_of_the_plan_is_kept(
    make_hospital, make_patient, add_reading, fake_model, bad_tip
):
    fake_model.reply_for["nutrition"] = plan_json(extra_tip=bad_tip)
    patient = make_patient(make_hospital("Scrub Hospital"))

    add_reading(patient, MEDIUM)

    nutrition = versions(current_plan(patient)).get(section="nutrition")
    assert nutrition.is_fallback is False
    assert nutrition.content["basis"] == "web_search"
    assert nutrition.content["timing_tips"] == ["Eat at regular times."]
    assert len(fake_model.calls_for("nutrition")) == 1  # no retry needed
    assert bad_tip not in str(nutrition.content)


def test_a_vegetarian_patient_is_never_given_meat_even_if_the_model_writes_it(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.reply_for["nutrition"] = plan_json(
        meals=[
            {"slot": "breakfast", "item_key": "oats", "text": "Oats with fruit"},
            {"slot": "dinner", "item_key": "chicken", "text": "Grilled chicken with roti"},
        ]
    )
    patient = make_patient(make_hospital("Vegetarian Hospital"), dietary_preference="vegetarian")

    add_reading(patient, MEDIUM)

    nutrition = versions(current_plan(patient)).get(section="nutrition")
    assert "chicken" not in str(nutrition.content["meals"]).lower()
    assert nutrition.content["meals"][0]["item_key"] == "oats"
    assert any("chicken" in r.lower() for r in nutrition.inputs["removed_for_allergy"])
