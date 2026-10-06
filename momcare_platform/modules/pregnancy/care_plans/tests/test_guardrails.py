"""The checks between the model's answer and a patient's screen.

Pure functions -- no database. Each protection is asserted with an input that
would get through if the protection were removed.
"""

import json
from typing import Any

import pytest

from momcare_platform.modules.pregnancy.care_plans.guardrails import (
    ADVICE_MAX_TIPS,
    AI_ONLY_SOURCE,
    BASIS_AI,
    BASIS_BASELINE,
    BASIS_WEB,
    EXERCISE_MAX_WORDS,
    FALLBACK_SOURCE,
    GENTLE_BASELINE_ACTIVITIES,
    LIST_LIMITS,
    NUTRITION_MAX_WORDS,
    InvalidPlan,
    banned_in_content,
    build_sources,
    cap_activity,
    excluded_foods,
    fallback_advice,
    fallback_content,
    finalize_advice,
    finalize_section,
    find_banned,
    normalize_item_key,
    parse_model_output,
    remove_allergens,
    strip_links,
    validate_schema,
    visible_word_count,
)


def plan(**overrides):
    base: dict[str, Any] = {
        "nutrition": {
            "meals": [
                {"slot": "breakfast", "item_key": "Paratha", "text": "Paratha with yogurt"},
                {"slot": "lunch", "item_key": "lentils", "text": "Lentils with rice"},
            ],
            "foods_to_eat": [{"item_key": "spinach", "text": "Spinach"}],
            "foods_to_avoid": [{"item_key": "raw_egg", "text": "Raw eggs"}],
            "hydration": "Drink water through the day",
            "timing_tips": ["Eat at regular times"],
            "weekly_rotation": [{"day": 1, "meals": ["Paratha with yogurt", "Lentils with rice"]}],
        },
        "exercise": {
            "activities": [
                {
                    "item_key": "walking",
                    "text": "Walk",
                    "duration_minutes": 20,
                    "frequency_per_week": 5,
                    "intensity": "light",
                }
            ],
            "avoid": [{"item_key": "heavy_lifting", "text": "Heavy lifting"}],
            "stop_signs": ["Dizziness"],
            "sources": ["WHO antenatal care guidance"],
        },
    }
    base["nutrition"]["sources"] = ["WHO antenatal care guidance"]
    for section in ("nutrition", "exercise"):
        base[section]["official_pages"] = [PAGES[0]["url"]]
    for path, value in overrides.items():
        section, key = path.split("__")
        base[section][key] = value
    return base


def raw(data) -> str:
    return json.dumps(data)


# What a live web search returned (the model never writes these).
PAGES = [{"title": "Pakistan Antenatal Care Strategy 2022-27", "url": "https://www.unicef.org/pakistan/guide.pdf"}]


# -- banned content ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Take an iron tablet daily",
        "Ask about a supplement",
        "Take folic acid every morning",
        "Prenatal vitamin with breakfast",
        "Take 60 mg of iron",
        "Around 2000 calories a day",
        "Aim for a weight target of 12 kg",
        "Your medication list",
        "A prescribed medicine",
        "Use the correct dosage",
    ],
)
def test_medicine_supplement_dose_and_calorie_wording_is_caught(text):
    assert find_banned(text), text


@pytest.mark.parametrize(
    "text",
    ["Spinach and lentils", "100 g of rice", "Vitamin C rich oranges", "A walk after dinner", "Foods rich in folate"],
)
def test_ordinary_food_and_exercise_wording_is_not_flagged(text):
    assert find_banned(text) == []


def test_banned_wording_is_found_anywhere_in_nested_content():
    data = plan(nutrition__timing_tips=["Eat early", "Remember your iron tablets"])
    assert banned_in_content(data)


# -- parsing and schema ------------------------------------------------------


def test_a_json_fence_around_the_answer_is_tolerated():
    assert parse_model_output("```json\n" + raw(plan()) + "\n```")["nutrition"]


def test_chatter_around_the_json_is_tolerated():
    assert parse_model_output("Here you go:\n" + raw(plan()) + "\nHope that helps!")["exercise"]


@pytest.mark.parametrize("bad", [None, "", "   ", "not json at all", "[1, 2, 3]"])
def test_unparseable_output_is_rejected(bad):
    with pytest.raises(InvalidPlan):
        parse_model_output(bad)


def test_missing_sections_are_rejected():
    with pytest.raises(InvalidPlan):
        validate_schema({"nutrition": {}})


def test_an_item_without_text_is_rejected():
    with pytest.raises(InvalidPlan):
        validate_schema(plan(nutrition__foods_to_eat=[{"item_key": "spinach"}]))


def test_an_empty_meal_list_is_rejected():
    with pytest.raises(InvalidPlan):
        validate_schema(plan(nutrition__meals=[]))


def test_item_keys_are_normalised_and_synonyms_folded():
    assert normalize_item_key("Oatmeal") == "oats"
    assert normalize_item_key("Rolled Oats!") == "oats"
    assert normalize_item_key("Brisk Walking") == "walking"
    assert normalize_item_key("Raw Egg") == "raw_egg"


def test_an_unknown_intensity_falls_back_to_light():
    data = plan(exercise__activities=[{"item_key": "walking", "text": "Walk", "intensity": "extreme"}])
    assert validate_schema(data)["exercise"]["activities"][0]["intensity"] == "light"


# -- allergies ---------------------------------------------------------------


def test_foods_matching_an_allergen_are_removed_from_what_she_is_told_to_eat():
    cleaned, removed = remove_allergens(validate_schema(plan()), ["yogurt"])
    meal_texts = [m["text"] for m in cleaned["nutrition"]["meals"]]
    assert "Paratha with yogurt" not in meal_texts
    assert "Lentils with rice" in meal_texts
    assert "Paratha with yogurt" in removed
    for day in cleaned["nutrition"]["weekly_rotation"]:
        assert all("yogurt" not in meal.lower() for meal in day["meals"])


def test_an_allergen_is_matched_by_plural_and_by_item_key():
    data = plan(nutrition__foods_to_eat=[{"item_key": "peanut_butter", "text": "A spread"}])
    cleaned, _ = remove_allergens(validate_schema(data), ["peanuts", "peanut"])
    assert cleaned["nutrition"]["foods_to_eat"] == []


def test_naming_an_allergen_under_foods_to_avoid_is_kept():
    data = plan(nutrition__foods_to_avoid=[{"item_key": "peanuts", "text": "Peanuts"}])
    cleaned, _ = remove_allergens(validate_schema(data), ["peanut"])
    assert cleaned["nutrition"]["foods_to_avoid"][0]["text"] == "Peanuts"


def test_no_allergens_changes_nothing():
    content = validate_schema(plan())
    cleaned, removed = remove_allergens(content, [])
    assert cleaned == content
    assert removed == []


def test_a_plan_where_every_meal_conflicts_is_rejected():
    data = plan(
        nutrition__meals=[{"slot": "breakfast", "item_key": "yogurt", "text": "Yogurt"}],
        nutrition__foods_to_eat=[],
        nutrition__weekly_rotation=[],
    )
    with pytest.raises(InvalidPlan):
        finalize_section(raw(data), "nutrition", risk="low", allergens=["yogurt"])


# -- activity caps -----------------------------------------------------------


def _activities(*items):
    return plan(exercise__activities=list(items))


def test_high_risk_keeps_only_the_gentle_baseline_set():
    data = validate_schema(
        _activities(
            {"item_key": "jogging", "text": "Jog", "duration_minutes": 40, "intensity": "vigorous"},
            {"item_key": "walking", "text": "Walk", "duration_minutes": 40, "intensity": "moderate"},
        )
    )
    capped = cap_activity(data, "high")
    assert [a["item_key"] for a in capped["exercise"]["activities"]] == ["walking"]
    walk = capped["exercise"]["activities"][0]
    assert walk["intensity"] == "light"
    assert walk["duration_minutes"] == 15


def test_high_risk_with_nothing_gentle_gets_a_baseline_walk():
    data = validate_schema(_activities({"item_key": "jogging", "text": "Jog", "intensity": "vigorous"}))
    capped = cap_activity(data, "high")
    assert [a["item_key"] for a in capped["exercise"]["activities"]] == ["walking"]


def test_medium_risk_is_light_and_at_most_thirty_minutes():
    data = validate_schema(
        _activities({"item_key": "cycling", "text": "Cycle", "duration_minutes": 60, "intensity": "vigorous"})
    )
    activity = cap_activity(data, "medium")["exercise"]["activities"][0]
    assert activity["intensity"] == "light"
    assert activity["duration_minutes"] == 30


def test_low_risk_allows_moderate_but_never_vigorous():
    data = validate_schema(
        _activities({"item_key": "cycling", "text": "Cycle", "duration_minutes": 90, "intensity": "vigorous"})
    )
    activity = cap_activity(data, "low")["exercise"]["activities"][0]
    assert activity["intensity"] == "moderate"
    assert activity["duration_minutes"] == 45


def test_an_unknown_risk_level_gets_the_strictest_treatment():
    data = validate_schema(_activities({"item_key": "jogging", "text": "Jog", "intensity": "vigorous"}))
    capped = cap_activity(data, "")
    assert all(a["item_key"] in GENTLE_BASELINE_ACTIVITIES for a in capped["exercise"]["activities"])


# -- end to end --------------------------------------------------------------


def test_finalize_returns_safe_content_for_a_good_answer():
    content, removed = finalize_section(raw(plan()), "nutrition", risk="medium", allergens=[], citations=PAGES)
    assert removed == []
    assert content["meals"][0]["item_key"] == "paratha"
    assert content["basis"] == BASIS_WEB


def test_finalize_drops_an_entry_that_mentions_a_supplement_but_keeps_the_plan():
    bad = plan(nutrition__timing_tips=["Take your iron supplement with orange juice", "Eat at regular times"])
    content, _ = finalize_section(raw(bad), "nutrition", risk="low", allergens=[], citations=PAGES)
    assert content["timing_tips"] == ["Eat at regular times"]
    assert banned_in_content(content) == []


def test_finalize_rejects_a_plan_whose_every_meal_carries_banned_wording():
    bad = plan(nutrition__meals=[{"slot": "breakfast", "item_key": "cereal", "text": "Cereal with folic acid"}])
    with pytest.raises(InvalidPlan, match="every meal"):
        finalize_section(raw(bad), "nutrition", risk="low", allergens=[], citations=PAGES)


def test_banned_hydration_text_is_replaced_by_a_neutral_line():
    bad = plan(nutrition__hydration="Drink water and take a vitamin tablet")
    content, _ = finalize_section(raw(bad), "nutrition", risk="low", allergens=[], citations=PAGES)
    assert content["hydration"] == "Drink water through the day."


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_the_fallback_plan_is_clean_and_respects_the_caps(risk):
    content = fallback_content(risk)
    assert banned_in_content(content) == []
    validate_schema(content)  # same shape the model must produce
    if risk == "high":
        assert all(a["item_key"] in GENTLE_BASELINE_ACTIVITIES for a in content["exercise"]["activities"])


# -- short plans --------------------------------------------------------------


def words(n: int) -> str:
    return " ".join(["word"] * n)


def test_a_normal_plan_is_well_inside_the_word_limits():
    data = validate_schema(plan())
    assert visible_word_count(data["nutrition"]) < NUTRITION_MAX_WORDS
    assert visible_word_count(data["exercise"]) < EXERCISE_MAX_WORDS


def test_keys_slots_and_numbers_are_not_counted_as_words():
    assert visible_word_count({"item_key": "a_b_c", "slot": "x y", "duration_minutes": 20, "text": "two words"}) == 2


def test_an_over_long_nutrition_section_is_rejected():
    long_tip = words(NUTRITION_MAX_WORDS + 1)
    with pytest.raises(InvalidPlan, match="too long"):
        finalize_section(raw(plan(nutrition__timing_tips=[long_tip])), "nutrition", risk="low", allergens=[])


def test_an_over_long_exercise_section_is_rejected():
    with pytest.raises(InvalidPlan, match="too long"):
        finalize_section(
            raw(plan(exercise__stop_signs=[words(EXERCISE_MAX_WORDS + 1)])), "exercise", risk="low", allergens=[]
        )


def test_a_plan_at_the_limit_is_accepted():
    content, _ = finalize_section(
        raw(plan(nutrition__timing_tips=[words(NUTRITION_MAX_WORDS - 40)])), "nutrition", risk="low", allergens=[]
    )
    assert visible_word_count(content) <= NUTRITION_MAX_WORDS


def test_over_long_lists_are_trimmed_to_their_limits():
    many = [{"item_key": f"food_{i}", "text": f"Food {i}"} for i in range(15)]
    data = validate_schema(
        plan(
            nutrition__foods_to_eat=many,
            nutrition__foods_to_avoid=many,
            nutrition__timing_tips=["tip"] * 9,
            exercise__stop_signs=["sign"] * 9,
        )
    )
    assert len(data["nutrition"]["foods_to_eat"]) == LIST_LIMITS["foods_to_eat"]
    assert len(data["nutrition"]["foods_to_avoid"]) == LIST_LIMITS["foods_to_avoid"]
    assert len(data["nutrition"]["timing_tips"]) == LIST_LIMITS["timing_tips"]
    assert len(data["exercise"]["stop_signs"]) == LIST_LIMITS["stop_signs"]


def test_a_weekly_rotation_from_the_model_is_dropped():
    assert validate_schema(plan())["nutrition"]["weekly_rotation"] == []


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_the_fallback_plan_fits_the_word_limits(risk):
    content = fallback_content(risk)
    assert visible_word_count(content["nutrition"]) <= NUTRITION_MAX_WORDS
    assert visible_word_count(content["exercise"]) <= EXERCISE_MAX_WORDS


# -- sources: recorded by code from the live web search ------------------------


def test_pages_the_search_returned_become_the_sources_with_their_links():
    result = build_sources(PAGES, [PAGES[0]["url"]])
    assert result["basis"] == BASIS_WEB
    assert result["source_links"] == [
        {"title": PAGES[0]["title"], "url": PAGES[0]["url"], "host": "unicef.org"},
    ]
    assert result["sources"] == ["Pakistan Antenatal Care Strategy 2022-27 (unicef.org)"]


def test_no_page_found_means_the_plan_says_it_was_generated_by_ai():
    for nothing in (None, [], [{"title": "x", "url": ""}], [{"title": "x", "url": "not a link"}], ["junk"]):
        result = build_sources(nothing)
        assert result == {"basis": BASIS_AI, "sources": [AI_ONLY_SOURCE], "source_links": []}
    assert "generated by AI" in AI_ONLY_SOURCE


def test_duplicate_pages_collapse_and_at_most_five_are_kept():
    cites = [{"title": f"Page {i}", "url": f"https://a.gov.pk/{i}"} for i in range(9)]
    result = build_sources(cites + cites, [c["url"] for c in cites])
    assert len(result["source_links"]) == 5
    assert len({link["url"] for link in result["source_links"]}) == 5


@pytest.mark.parametrize(
    "title",
    ["", "(c) junk", "(c) Designed By Human Design Studios", "A) Professor Someone B) Dr Else", "x" * 200],
)
def test_a_junk_page_title_is_replaced_by_the_site_name(title):
    title = title.replace("(c)", "\u00a9")
    url = "https://www.health.gov.pk/doc.pdf"
    result = build_sources([{"title": title, "url": url}], [url])
    assert result["source_links"][0]["title"] == "health.gov.pk"
    assert result["sources"] == ["health.gov.pk"]


def test_a_page_title_mentioning_supplements_keeps_the_link_but_shows_the_site_name():
    result = build_sources(
        [{"title": "Iron supplementation guideline", "url": "https://who.int/iron"}], ["https://who.int/iron"]
    )
    assert result["source_links"][0]["url"] == "https://who.int/iron"
    assert result["source_links"][0]["title"] == "who.int"


def test_the_models_own_claimed_sources_are_ignored():
    """A model invents document titles. Only what the search returned is recorded."""
    data = plan()
    data["nutrition"]["sources"] = ["WHO antenatal care guidance", "An invented 2031 guideline"]
    content, _ = finalize_section(raw(data), "nutrition", risk="low", allergens=[], citations=[])
    assert content["basis"] == BASIS_AI
    assert content["sources"] == [AI_ONLY_SOURCE]


def test_links_and_markdown_citations_the_model_wrote_into_its_text_are_removed():
    data = plan(
        nutrition__timing_tips=[
            "Eat at regular times ([unicef.org](https://www.unicef.org/x.pdf)).",
            "Drink water, see https://example.com/blog for more.",
        ]
    )
    content, _ = finalize_section(raw(data), "nutrition", risk="low", allergens=[], citations=PAGES)
    assert content["timing_tips"] == ["Eat at regular times.", "Drink water, see for more."]
    assert strip_links("Plain text stays.") == "Plain text stays."


def test_the_same_pages_come_through_for_exercise_too():
    content, _ = finalize_section(raw(plan()), "exercise", risk="low", allergens=[], citations=PAGES)
    assert content["basis"] == BASIS_WEB
    assert content["source_links"][0]["url"] == PAGES[0]["url"]
    assert content["activities"][0]["item_key"] == "walking"


def test_an_answer_may_be_wrapped_in_its_own_section_name_or_bare():
    wrapped = finalize_section(raw({"exercise": plan()["exercise"]}), "exercise", risk="low", allergens=[])[0]
    bare = finalize_section(raw(plan()["exercise"]), "exercise", risk="low", allergens=[])[0]
    assert wrapped == bare


def test_an_unknown_section_is_refused():
    with pytest.raises(InvalidPlan):
        finalize_section(raw(plan()), "medication", risk="low", allergens=[])


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_the_fallback_sections_each_name_the_baseline_as_their_source(risk):
    content = fallback_content(risk)
    for section in ("nutrition", "exercise"):
        assert content[section]["sources"] == [FALLBACK_SOURCE]
        assert content[section]["basis"] == BASIS_BASELINE
        assert content[section]["source_links"] == []


def test_sources_do_not_count_towards_the_word_limit():
    assert visible_word_count({"text": "two words", "sources": [words(100)]}) == 2


# -- the fallback must suit vegetarians and vegans ----------------------------

ANIMAL_FOODS = (
    "meat",
    "chicken",
    "mutton",
    "beef",
    "fish",
    "egg",
    "milk",
    "yogurt",
    "dahi",
    "curd",
    "cheese",
    "dairy",
)


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_the_fallback_never_tells_anyone_to_eat_meat_fish_egg_or_dairy(risk):
    nutrition = fallback_content(risk)["nutrition"]
    eaten = " ".join(m["text"] + " " + m["item_key"] for m in nutrition["meals"])
    eaten += " " + " ".join(i["text"] + " " + i["item_key"] for i in nutrition["foods_to_eat"])
    assert not [w for w in ANIMAL_FOODS if w in eaten.lower()], eaten


# -- reading advice -----------------------------------------------------------


def advice(tips=None, *, contact=False, sources=("WHO antenatal care guidance",)):
    data = {"tips": tips if tips is not None else ["Rest and drink water.", "Avoid heavy activity today."]}
    data["contact_care_team"] = contact
    if sources is not None:
        data["sources"] = list(sources)
    return json.dumps(data)


def test_good_advice_comes_through_carrying_the_weekly_plans_sources():
    result = finalize_advice(advice(), risk="medium", allergens=[], sources=build_sources(PAGES, [PAGES[0]["url"]]))
    assert result["tips"] == ["Rest and drink water.", "Avoid heavy activity today."]
    assert result["basis"] == BASIS_WEB
    assert result["source_links"][0]["url"] == PAGES[0]["url"]
    assert result["contact_care_team"] is False


def test_advice_with_no_plan_sources_says_it_was_generated_by_ai():
    result = finalize_advice(advice(), risk="medium", allergens=[])
    assert result["basis"] == BASIS_AI
    assert result["sources"] == [AI_ONLY_SOURCE]


def test_high_risk_advice_always_says_to_contact_the_care_team():
    assert finalize_advice(advice(contact=False), risk="high", allergens=[])["contact_care_team"] is True


def test_the_model_can_ask_for_contact_at_medium_risk():
    assert finalize_advice(advice(contact=True), risk="medium", allergens=[])["contact_care_team"] is True


@pytest.mark.parametrize(
    "tip",
    [
        "Take an iron tablet with lunch.",
        "A supplement may help.",
        "Eat about 1800 calories today.",
        "Ask for a 500 mg dose.",
    ],
)
def test_advice_may_not_mention_medicines_supplements_doses_or_calories(tip):
    with pytest.raises(InvalidPlan, match="banned"):
        finalize_advice(advice([tip]), risk="medium", allergens=[])


@pytest.mark.parametrize(
    "tip", ["Go for a jog.", "Try swimming laps.", "Do a vigorous workout.", "Lift the shopping bags yourself."]
)
def test_strenuous_tips_are_dropped_at_high_risk_but_allowed_to_pass_at_low_risk_checks(tip):
    result = finalize_advice(advice([tip, "Rest now."]), risk="high", allergens=[])
    assert result["tips"] == ["Rest now."]


def test_if_every_tip_is_strenuous_at_high_risk_the_advice_is_invalid():
    with pytest.raises(InvalidPlan, match="no usable tip"):
        finalize_advice(advice(["Go for a run."]), risk="high", allergens=[])


def test_a_tip_telling_her_to_eat_an_allergen_is_dropped_but_telling_her_to_avoid_it_is_kept():
    result = finalize_advice(
        advice(["Have some yogurt.", "Avoid yogurt and cheese today.", "Drink water."]),
        risk="medium",
        allergens=["yogurt"],
    )
    assert result["tips"] == ["Avoid yogurt and cheese today.", "Drink water."]


def test_at_most_four_tips_are_kept():
    result = finalize_advice(advice([f"Tip number {i}." for i in range(9)]), risk="medium", allergens=[])
    assert len(result["tips"]) == ADVICE_MAX_TIPS


def test_a_tip_that_is_too_long_makes_the_advice_invalid():
    with pytest.raises(InvalidPlan, match="too long"):
        finalize_advice(advice([words(31)]), risk="medium", allergens=[])


@pytest.mark.parametrize("bad", [None, "", "not json", '{"tips": "rest"}', '{"tips": [1, 2], "sources": ["WHO"]}'])
def test_unparseable_advice_is_rejected(bad):
    with pytest.raises(InvalidPlan):
        finalize_advice(bad, risk="medium", allergens=[])


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_the_fallback_advice_is_clean_names_its_source_and_contacts_the_care_team_at_high_risk(risk):
    result = fallback_advice(risk)
    assert result["sources"] == [FALLBACK_SOURCE]
    assert result["basis"] == BASIS_BASELINE
    assert find_banned(" ".join(result["tips"])) == []
    assert result["contact_care_team"] is (risk == "high")
    if risk == "high":
        assert not any("jog" in t.lower() or "run" in t.lower() for t in result["tips"])


# -- a list entry must name the food ------------------------------------------


def test_a_reason_without_the_food_gets_the_food_put_in_front():
    data = validate_schema(
        plan(
            nutrition__foods_to_eat=[
                {"item_key": "spinach", "text": "Rich in iron, helps with hemoglobin."},
                {"item_key": "lentils", "text": "Lentils for protein and iron"},
                {"item_key": "roti", "text": "Chapati"},
            ]
        )
    )
    texts = [i["text"] for i in data["nutrition"]["foods_to_eat"]]
    assert texts == ["Spinach: Rich in iron, helps with hemoglobin.", "Lentils for protein and iron", "Chapati"]


# -- which pages count as official sources ---------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "health.gov.pk",
        "moh.go.ke",
        "nhs.uk",
        "who.int",
        "unicef.org",
        "sogp.org",
        "cdc.gov",
        "ox.ac.uk",
        "harvard.edu",
    ],
)
def test_government_international_and_society_sites_are_official(host):
    from momcare_platform.modules.pregnancy.care_plans.guardrails import is_official_host  # noqa: PLC0415

    assert is_official_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["holisticare.pk", "umeed-mama-guide.lovable.app", "bumpbites.health", "exa.ai", "go.com", "babycenter.com"],
)
def test_clinic_blog_app_and_shop_sites_are_never_official(host):
    from momcare_platform.modules.pregnancy.care_plans.guardrails import is_official_host  # noqa: PLC0415

    assert is_official_host(host) is False


def test_only_pages_the_model_says_it_used_and_that_are_official_are_listed():
    cites = [
        {"title": "Ministry guide", "url": "https://www.health.gov.pk/guide.pdf"},
        {"title": "A clinic blog", "url": "https://holisticare.pk/diet"},
        {"title": "Unused ministry page", "url": "https://www.mohp.gov.example/other"},
    ]
    chosen = [
        "https://www.health.gov.pk/guide.pdf",
        "https://holisticare.pk/diet",
        "https://invented.gov.pk/never-searched",
    ]
    result = build_sources(cites, chosen)
    assert [link["url"] for link in result["source_links"]] == ["https://www.health.gov.pk/guide.pdf"]
    assert result["basis"] == BASIS_WEB


def test_when_no_official_page_was_used_the_plan_says_generated_by_ai_even_though_pages_were_read():
    cites = [{"title": "A clinic blog", "url": "https://holisticare.pk/diet"}]
    for chosen in (None, [], ["https://holisticare.pk/diet"]):
        assert build_sources(cites, chosen)["basis"] == BASIS_AI


# -- vegetarian and vegan are enforced in code, like an allergy ------------------


def _diet_plan():
    return plan(
        nutrition__meals=[
            {"slot": "breakfast", "item_key": "scrambled_eggs", "text": "Scrambled eggs with roti"},
            {"slot": "lunch", "item_key": "lentils", "text": "Lentil curry with rice"},
            {"slot": "dinner", "item_key": "chicken_curry", "text": "Chicken curry with roti"},
        ],
        nutrition__foods_to_eat=[
            {"item_key": "chicken", "text": "Chicken: high-quality protein"},
            {"item_key": "spinach", "text": "Spinach: iron"},
            {"item_key": "yogurt", "text": "Yogurt: calcium"},
        ],
    )


def test_excluded_foods_combines_allergies_with_the_diet():
    assert excluded_foods(["peanut"], "none") == ["peanut"]
    assert "chicken" in excluded_foods([], "vegetarian") and "egg" not in excluded_foods([], "vegetarian")
    assert {"chicken", "egg", "yogurt", "honey"} <= set(excluded_foods(["peanut"], "vegan"))
    assert "peanut" in excluded_foods(["peanut"], "vegan")


def test_a_vegetarian_is_never_told_to_eat_meat_or_fish_whatever_the_model_wrote():
    content, removed = finalize_section(
        raw(_diet_plan()), "nutrition", risk="low", allergens=excluded_foods([], "vegetarian"), citations=PAGES
    )
    eaten = " ".join(i["text"] for i in content["meals"] + content["foods_to_eat"]).lower()
    assert "chicken" not in eaten
    assert "eggs" in eaten and "yogurt" in eaten  # a vegetarian may eat these
    assert any("Chicken" in r for r in removed)


def test_a_vegan_is_also_never_told_to_eat_eggs_or_dairy():
    content, _ = finalize_section(
        raw(_diet_plan()), "nutrition", risk="low", allergens=excluded_foods([], "vegan"), citations=PAGES
    )
    eaten = " ".join(i["text"] for i in content["meals"] + content["foods_to_eat"]).lower()
    assert not any(word in eaten for word in ("chicken", "egg", "yogurt"))
    assert "lentil" in eaten and "spinach" in eaten


def test_no_dietary_preference_removes_nothing():
    content, removed = finalize_section(raw(_diet_plan()), "nutrition", risk="low", allergens=[], citations=PAGES)
    assert removed == []
    assert "Chicken: high-quality protein" in [i["text"] for i in content["foods_to_eat"]]


# -- what the production review found -------------------------------------------


NUTRITION_PAGE = {"title": "Guidelines for Maternal Nutrition", "url": "https://sogp.org/maternal-nutrition.pdf"}
EXERCISE_PAGE = {"title": "Physical activity in pregnancy", "url": "https://www.health.gov.pk/physical-activity.pdf"}
GENERAL_PAGE = {"title": "WHO recommendations on antenatal care", "url": "https://www.who.int/antenatal-care.pdf"}


def test_a_nutrition_guideline_is_not_a_source_for_the_exercise_plan_and_the_reverse():
    chosen = [p["url"] for p in (NUTRITION_PAGE, EXERCISE_PAGE, GENERAL_PAGE)]
    pages = [NUTRITION_PAGE, EXERCISE_PAGE, GENERAL_PAGE]

    exercise = build_sources(pages, chosen, "exercise")
    nutrition = build_sources(pages, chosen, "nutrition")

    assert [link["url"] for link in exercise["source_links"]] == [EXERCISE_PAGE["url"], GENERAL_PAGE["url"]]
    assert [link["url"] for link in nutrition["source_links"]] == [NUTRITION_PAGE["url"], GENERAL_PAGE["url"]]


def test_an_exercise_plan_whose_only_page_is_a_nutrition_guideline_says_generated_by_ai():
    result = build_sources([NUTRITION_PAGE], [NUTRITION_PAGE["url"]], "exercise")
    assert result["basis"] == BASIS_AI and result["source_links"] == []


def test_without_a_section_every_official_page_counts():
    assert build_sources([NUTRITION_PAGE], [NUTRITION_PAGE["url"]])["basis"] == BASIS_WEB


def test_underscores_a_model_copies_from_the_key_into_the_text_are_removed():
    data = validate_schema(
        plan(
            nutrition__meals=[
                {"slot": "lunch", "item_key": "chapati", "text": "whole-wheat_chapati with grilled_chicken"}
            ]
        )
    )
    assert data["nutrition"]["meals"][0]["text"] == "whole-wheat chapati with grilled chicken"
    assert data["nutrition"]["meals"][0]["item_key"] == "roti"  # chapati is folded into roti


@pytest.mark.parametrize("risk", ["medium", "high"])
def test_nothing_brisk_or_fast_is_left_in_an_activity_at_medium_or_high_risk(risk):
    data = validate_schema(
        _activities(
            {"item_key": "walking", "text": "Brisk walking", "intensity": "moderate", "duration_minutes": 30},
            {
                "item_key": "walking",
                "text": "Fast-paced walking for fitness",
                "intensity": "light",
                "duration_minutes": 10,
            },
        )
    )
    texts = [a["text"] for a in cap_activity(data, risk)["exercise"]["activities"]]
    assert texts == ["Walking", "Walking for fitness"]


def test_a_low_risk_patient_keeps_the_wording_the_model_wrote():
    data = validate_schema(
        _activities({"item_key": "walking", "text": "Brisk walking", "intensity": "moderate", "duration_minutes": 30})
    )
    assert cap_activity(data, "low")["exercise"]["activities"][0]["text"] == "Brisk walking"
