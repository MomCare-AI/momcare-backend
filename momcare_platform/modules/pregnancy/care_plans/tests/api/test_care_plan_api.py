"""The care plan API: reading, tenancy, staff edits, status and the patient's view."""

import json

import pytest
from django.conf import settings

from momcare_platform.modules.pregnancy.care_plans.models import CarePlan, PlanCorrection
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import (
    CITATIONS,
    HIGH,
    LOW,
    MEDIUM,
    SOURCE_NAME,
    current_plan,
)

pytestmark = pytest.mark.django_db


def call(client, method, url, data=None, **headers):
    kwargs = {"content_type": "application/json", **headers}
    if data is not None:
        kwargs["data"] = json.dumps(data)
    return getattr(client, method)(url, **kwargs)


def plan_url(plan):
    return f"/api/care-plans/{plan.id}/"


@pytest.fixture
def setup(make_hospital, make_staff, make_patient, add_reading, fake_model, auth):
    """One hospital, a patient with a medium-risk plan, and one staff user per role."""
    hospital = make_hospital("Plan Hospital")
    patient = make_patient(hospital)
    add_reading(patient, MEDIUM)
    users = {
        "provider": make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@planhospital.test"),
        "nurse": make_staff(hospital.org, settings.ROLE_NURSE, "nurse@planhospital.test"),
        "care_manager": make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@planhospital.test"),
    }
    return type(
        "S",
        (),
        {
            "hospital": hospital,
            "patient": patient,
            "plan": current_plan(patient),
            "users": users,
            "h": lambda self, who: auth(hospital.admin.email if who == "admin" else users[who].email, "TestPass!2026"),
        },
    )()


# -- reading -----------------------------------------------------------------


def test_the_plan_detail_is_one_aggregate_response(client, setup):
    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()

    assert body["month_number"] == 5
    assert body["trimester"] == 2
    assert body["weeks_label"] == "weeks 17-21"
    assert body["status"] == "in_progress"
    assert body["is_current_month"] is True
    assert body["nutrition"]["content"]["meals"][0]["item_key"] == "oats"
    assert body["nutrition"]["label"] == "suggested_automatically"
    assert body["nutrition"]["sources"] == [SOURCE_NAME]
    assert body["exercise"]["sources"] == [SOURCE_NAME]
    # The links are the pages the live search returned, recorded by code.
    assert body["nutrition"]["basis"] == "web_search"
    assert [link["url"] for link in body["nutrition"]["source_links"]] == [CITATIONS[0]["url"]]
    assert body["exercise"]["source_links"][0]["host"] == "health.gov.example"
    assert body["nutrition"]["generated_by_ai_notice"] is None
    assert body["nutrition"]["sources_verified"] is False  # the content is not checked against the pages
    assert body["nutrition"]["generated_at"] is not None
    assert body["exercise"]["generated_at"] is not None
    assert body["exercise"]["content"]["activities"][0]["item_key"] == "walking"
    assert body["medications"] == []
    assert body["notes"] == []
    assert body["allergies_and_conditions"]["food_allergies"] == []
    assert "disclaimer" in body
    assert body["warning_signs"]
    assert body["clinical_review"] == "unreviewed"


def test_the_list_filters_by_pregnancy_and_uses_the_standard_envelope(client, setup):
    pregnancy = setup.patient.current_pregnancy
    body = call(client, "get", f"/api/care-plans/?pregnancy={pregnancy.id}", **setup.h("provider")).json()
    assert body["count"] == 1
    assert {"page", "page_size", "total_pages", "next", "previous", "results"} <= set(body)


def test_current_care_plan_for_a_pregnancy(client, setup):
    pregnancy = setup.patient.current_pregnancy
    body = call(client, "get", f"/api/pregnancies/{pregnancy.id}/current-care-plan/", **setup.h("nurse")).json()
    assert body["care_plan"]["id"] == str(setup.plan.id)


def test_get_never_creates_a_plan(client, make_hospital, make_patient, make_staff, auth):
    hospital = make_hospital("No Reading Hospital")
    patient = make_patient(hospital)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "n@noreading.test")

    body = call(
        client, "get", f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/", **auth(nurse.email)
    ).json()

    # No readings: nothing is on its way, so she is NOT told a plan is being prepared.
    assert body == {"care_plan": None, "preparing": False, "update_pending": False, "status_message": None}
    assert not CarePlan.objects.exists()


def test_each_staff_role_can_read_a_plan(client, setup):
    for role in ("admin", "provider", "nurse", "care_manager"):
        assert call(client, "get", plan_url(setup.plan), **setup.h(role)).status_code == 200


# -- tenancy -----------------------------------------------------------------


@pytest.fixture
def outsider(make_hospital, auth):
    other = make_hospital("Other Hospital")
    return auth(other.admin.email)


def test_another_hospital_gets_404_on_every_plan_endpoint(client, setup, outsider):
    plan = setup.plan
    pregnancy = setup.patient.current_pregnancy
    urls = [
        ("get", plan_url(plan)),
        ("get", f"/api/pregnancies/{pregnancy.id}/current-care-plan/"),
        ("post", f"{plan_url(plan)}adjustments/"),
        ("post", f"{plan_url(plan)}notes/"),
        ("post", f"{plan_url(plan)}review/"),
        ("patch", f"{plan_url(plan)}allergies-conditions/"),
    ]
    for method, url in urls:
        body = {
            "text": "x",
            "section": "nutrition",
            "list_name": "meals",
            "action": "remove",
            "item_key": "oats",
            "dietary_preference": "vegan",
        }
        response = call(client, method, url, None if method == "get" else body, **outsider)
        assert response.status_code == 404, (method, url)


def test_another_hospitals_list_is_empty(client, setup, outsider):
    assert call(client, "get", "/api/care-plans/", **outsider).json()["count"] == 0


def test_a_malformed_id_is_a_404_not_a_server_error(client, setup):
    assert call(client, "get", "/api/care-plans/not-a-uuid/", **setup.h("nurse")).status_code == 404
    bad = "/api/care-plans/" + "0" * 8 + "-0000-0000-0000-" + "0" * 12 + "/"
    assert call(client, "get", bad, **setup.h("nurse")).status_code == 404


def test_unauthenticated_requests_are_refused(client, setup):
    assert call(client, "get", plan_url(setup.plan)).status_code in (401, 403)


# -- the patient's view ------------------------------------------------------


def test_a_patient_reads_her_own_plan_without_staff_bookkeeping(client, setup, patient_user, auth):
    mother = patient_user(setup.patient)
    pregnancy = setup.patient.current_pregnancy

    response = call(
        client, "get", f"/api/pregnancies/{pregnancy.id}/current-care-plan/", **auth(mother.email, mother.password)
    )

    assert response.status_code == 200
    plan = response.json()["care_plan"]
    assert plan["nutrition"]["content"]["meals"]
    assert plan["exercise"]["content"]["activities"]
    for staff_only in ("reviewed_by", "finalized_by", "clinical_review", "last_evaluated_at"):
        assert staff_only not in plan


def test_a_patient_cannot_read_another_patients_plan(client, setup, patient_user, make_patient, add_reading, auth):
    mother = patient_user(setup.patient)
    stranger = make_patient(setup.hospital, first_name="Stranger")
    add_reading(stranger, MEDIUM)

    response = call(
        client,
        "get",
        f"/api/pregnancies/{stranger.current_pregnancy.id}/current-care-plan/",
        **auth(mother.email, mother.password),
    )

    assert response.status_code == 404


def test_a_patient_cannot_use_the_staff_endpoints_or_write_anything(client, setup, patient_user, auth):
    mother = patient_user(setup.patient)
    headers = auth(mother.email, mother.password)

    assert call(client, "get", plan_url(setup.plan), **headers).status_code == 403
    assert call(client, "get", "/api/care-plans/", **headers).status_code == 403
    assert call(client, "post", f"{plan_url(setup.plan)}notes/", {"text": "hi"}, **headers).status_code == 403
    assert call(client, "post", f"{plan_url(setup.plan)}medications/", {"text": "x"}, **headers).status_code == 403
    assert call(client, "post", f"{plan_url(setup.plan)}review/", **headers).status_code == 403
    assert (
        call(
            client, "patch", f"{plan_url(setup.plan)}allergies-conditions/", {"dietary_preference": "vegan"}, **headers
        ).status_code
        == 403
    )


def test_the_patient_sees_the_note_text_and_the_doctors_medication(client, setup, patient_user, auth):
    call(client, "post", f"{plan_url(setup.plan)}notes/", {"text": "Rest more this week."}, **setup.h("nurse"))
    call(
        client,
        "post",
        f"{plan_url(setup.plan)}medications/",
        {"text": "As prescribed in clinic"},
        **setup.h("provider"),
    )
    mother = patient_user(setup.patient)
    pregnancy = setup.patient.current_pregnancy

    plan = call(
        client, "get", f"/api/pregnancies/{pregnancy.id}/current-care-plan/", **auth(mother.email, mother.password)
    ).json()["care_plan"]

    assert [n["text"] for n in plan["notes"]] == ["Rest more this week."]
    assert [m["text"] for m in plan["medications"]] == ["As prescribed in clinic"]


def test_the_plan_tells_the_patient_which_week_it_is_for_and_to_keep_following_it(client, setup):
    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()
    assert body["message"] == (
        "This is your plan for week 20. Keep following it; we will tell you if anything needs to change."
    )
    assert body["week"]["week_number"] == 20
    assert body["week"]["replans"] == 0


# -- medications (provider only) and notes (all staff) -----------------------


def test_only_a_provider_can_write_medication(client, setup):
    url = f"{plan_url(setup.plan)}medications/"
    for role in ("nurse", "care_manager", "admin"):
        assert call(client, "post", url, {"text": "x"}, **setup.h(role)).status_code == 403, role
    response = call(client, "post", url, {"text": "As prescribed"}, **setup.h("provider"))
    assert response.status_code == 201
    assert [m["text"] for m in response.json()["medications"]] == ["As prescribed"]


def test_medication_can_be_edited_and_deactivated_never_deleted(client, setup):
    created = call(client, "post", f"{plan_url(setup.plan)}medications/", {"text": "First"}, **setup.h("provider"))
    med_id = created.json()["medications"][0]["id"]
    url = f"{plan_url(setup.plan)}medications/{med_id}/"

    edited = call(client, "patch", url, {"text": "Second"}, **setup.h("provider")).json()
    assert [m["text"] for m in edited["medications"]] == ["Second"]

    gone = call(client, "delete", url, {"reason": "stopped"}, **setup.h("provider")).json()
    assert gone["medications"] == []
    from momcare_platform.modules.pregnancy.care_plans.models import CarePlanMedication  # noqa: PLC0415

    row = CarePlanMedication.objects.get(pk=med_id)
    assert row.is_active is False
    assert row.deactivation_reason == "stopped"
    assert row.deactivated_by is not None


def test_a_nurse_cannot_edit_or_deactivate_a_medication(client, setup):
    created = call(client, "post", f"{plan_url(setup.plan)}medications/", {"text": "x"}, **setup.h("provider"))
    url = f"{plan_url(setup.plan)}medications/{created.json()['medications'][0]['id']}/"
    assert call(client, "patch", url, {"text": "y"}, **setup.h("nurse")).status_code == 403
    assert call(client, "delete", url, **setup.h("nurse")).status_code == 403


def test_any_staff_role_can_write_notes_and_deactivate_them(client, setup):
    for role in ("admin", "provider", "nurse", "care_manager"):
        response = call(client, "post", f"{plan_url(setup.plan)}notes/", {"text": f"from {role}"}, **setup.h(role))
        assert response.status_code == 201, role
    notes = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()["notes"]
    assert len(notes) == 4
    url = f"{plan_url(setup.plan)}notes/{notes[0]['id']}/"
    assert len(call(client, "delete", url, **setup.h("nurse")).json()["notes"]) == 3


def test_a_blank_note_is_rejected(client, setup):
    assert (
        call(client, "post", f"{plan_url(setup.plan)}notes/", {"text": "   "}, **setup.h("nurse")).status_code == 400
    )


def test_a_note_belonging_to_another_plan_is_not_reachable_through_this_one(client, setup, make_patient, add_reading):
    other_patient = make_patient(setup.hospital, first_name="Other")
    add_reading(other_patient, MEDIUM)
    other_plan = current_plan(other_patient)
    note = call(client, "post", f"{plan_url(other_plan)}notes/", {"text": "private"}, **setup.h("nurse"))
    note_id = note.json()["notes"][0]["id"]

    response = call(client, "delete", f"{plan_url(setup.plan)}notes/{note_id}/", **setup.h("nurse"))

    assert response.status_code == 404


# -- editing the generated sections ------------------------------------------


def adjust(client, setup, who="nurse", **body):
    return call(client, "post", f"{plan_url(setup.plan)}adjustments/", body, **setup.h(who))


def meal_keys(response):
    return [m["item_key"] for m in response.json()["nutrition"]["content"]["meals"]]


def test_removing_a_generated_item_hides_it_and_logs_a_correction(client, setup):
    response = adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")

    assert response.status_code == 201
    assert meal_keys(response) == ["lentils"]
    correction = PlanCorrection.objects.get()
    assert correction.action == "remove"
    assert correction.item_key == "oats"
    assert correction.original["text"] == "Oats with milk"
    assert correction.organization == setup.hospital.org
    assert correction.region == "asia"
    assert correction.trimester == 2


def test_replacing_a_generated_item_marks_it_as_staff_written(client, setup):
    response = adjust(
        client,
        setup,
        section="nutrition",
        list_name="meals",
        action="edit",
        item_key="oats",
        content={"text": "Paratha with yogurt", "slot": "breakfast"},
    )

    meals = response.json()["nutrition"]["content"]["meals"]
    edited = next(m for m in meals if m["item_key"] == "oats")
    assert edited["text"] == "Paratha with yogurt"
    assert edited["source"] == "staff"
    assert PlanCorrection.objects.get().replacement["text"] == "Paratha with yogurt"


def test_staff_can_add_their_own_item_and_it_is_not_counted_as_a_correction_of_anything(client, setup):
    response = adjust(
        client,
        setup,
        section="exercise",
        list_name="activities",
        action="add",
        item_key="",
        content={"text": "Prenatal yoga", "duration_minutes": 15, "intensity": "light"},
    )

    keys = [a["item_key"] for a in response.json()["exercise"]["content"]["activities"]]
    assert "prenatal_yoga" not in keys  # normalised to the standard key below
    assert "gentle_yoga" in keys
    assert PlanCorrection.objects.get().action == "add"


def test_a_doctor_may_widen_the_activity_the_automatic_cap_would_have_limited(client, setup, add_reading):
    add_reading(setup.patient, HIGH)  # a high-risk plan is capped; a clinician's own choice is not
    response = adjust(
        client,
        setup,
        who="provider",
        section="exercise",
        list_name="activities",
        action="add",
        content={"text": "Swimming", "duration_minutes": 30, "intensity": "moderate"},
    )

    swim = next(a for a in response.json()["exercise"]["content"]["activities"] if a["item_key"] == "swimming")
    assert swim["intensity"] == "moderate"
    assert swim["duration_minutes"] == 30


def test_medicine_supplement_dose_or_calorie_text_is_refused_in_an_edit(client, setup):
    for text in ("Take an iron tablet", "Eat 2000 calories", "Folic acid daily", "A 500 mg supplement"):
        response = adjust(
            client,
            setup,
            section="nutrition",
            list_name="meals",
            action="edit",
            item_key="oats",
            content={"text": text},
        )
        assert response.status_code == 400, text
    assert not PlanCorrection.objects.exists()


def test_editing_an_item_that_is_not_in_the_plan_is_refused(client, setup):
    response = adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="pizza")
    assert response.status_code == 400


def test_an_unknown_section_or_list_is_refused(client, setup):
    assert (
        adjust(
            client, setup, section="nutrition", list_name="activities", action="remove", item_key="oats"
        ).status_code
        == 400
    )
    assert (
        adjust(client, setup, section="medication", list_name="meals", action="remove", item_key="oats").status_code
        == 400
    )


def test_deactivating_a_removal_brings_the_generated_item_back(client, setup):
    from momcare_platform.modules.pregnancy.care_plans.models import CarePlanAdjustment  # noqa: PLC0415

    adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")
    adjustment = CarePlanAdjustment.objects.get()

    response = call(client, "delete", f"{plan_url(setup.plan)}adjustments/{adjustment.id}/", **setup.h("nurse"))

    assert "oats" in meal_keys(response)
    adjustment.refresh_from_db()
    assert adjustment.is_active is False


def test_a_staff_added_item_can_be_reworded_through_its_own_adjustment(client, setup):
    from momcare_platform.modules.pregnancy.care_plans.models import CarePlanAdjustment  # noqa: PLC0415

    adjust(client, setup, section="nutrition", list_name="foods_to_eat", action="add", content={"text": "Dates"})
    adjustment = CarePlanAdjustment.objects.get()
    response = call(
        client,
        "patch",
        f"{plan_url(setup.plan)}adjustments/{adjustment.id}/",
        {"content": {"text": "Dates and almonds"}},
        **setup.h("nurse"),
    )
    items = response.json()["nutrition"]["content"]["foods_to_eat"]
    assert "Dates and almonds" in [i["text"] for i in items]


def test_a_doctors_edit_survives_a_regeneration_and_a_removed_item_is_not_shown_again(
    client, setup, add_reading, fake_model
):
    adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")
    adjust(
        client,
        setup,
        section="nutrition",
        list_name="meals",
        action="edit",
        item_key="lentils",
        content={"text": "Daal with brown rice"},
    )

    call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["peanut"]},
        **setup.h("nurse"),
    )  # a newly recorded allergy re-plans the week: the model is asked again, and offers oats again
    assert fake_model.count == 2

    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()
    meals = body["nutrition"]["content"]["meals"]
    assert [m["item_key"] for m in meals] == ["lentils"]
    assert meals[0]["text"] == "Daal with brown rice"


def test_the_prompt_carries_the_patients_own_doctor_edits_as_data(client, setup, add_reading, fake_model):
    adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")

    call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["peanut"]},
        **setup.h("nurse"),
    )

    prompt = fake_model.calls_for("nutrition")[-1]
    assert "<this_patients_doctor_edits_data>" in prompt
    assert "removed oats from meals" in prompt


def test_an_unchanged_patient_keeps_the_plan_the_doctor_approved_without_a_new_model_call(
    client, setup, add_reading, fake_model
):
    adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")
    before = fake_model.count

    add_reading(setup.patient, MEDIUM)  # same condition as the plan

    assert fake_model.count == before
    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()
    assert [m["item_key"] for m in body["nutrition"]["content"]["meals"]] == ["lentils"]


# -- status ------------------------------------------------------------------


def post(client, setup, who, action):
    return call(client, "post", f"{plan_url(setup.plan)}{action}/", **setup.h(who))


def test_status_moves_in_progress_to_reviewed_to_finalized_with_who_and_when(client, setup):
    reviewed = post(client, setup, "nurse", "review").json()
    assert reviewed["status"] == "reviewed"
    assert reviewed["reviewed_by"]
    assert reviewed["reviewed_at"]
    assert reviewed["nutrition"]["label"] == "reviewed_by_provider"

    finalized = post(client, setup, "provider", "finalize").json()
    assert finalized["status"] == "finalized"
    assert finalized["finalized_by"]
    assert finalized["finalized_at"]


def test_status_steps_cannot_be_skipped_or_repeated(client, setup):
    assert post(client, setup, "provider", "finalize").status_code == 409  # not reviewed yet
    post(client, setup, "nurse", "review")
    assert post(client, setup, "nurse", "review").status_code == 409
    assert post(client, setup, "provider", "reopen").status_code == 200
    assert post(client, setup, "provider", "reopen").status_code == 409  # already in progress


def test_only_a_provider_or_hospital_admin_can_finalize_or_reopen(client, setup):
    post(client, setup, "nurse", "review")
    for who in ("nurse", "care_manager"):
        assert post(client, setup, who, "finalize").status_code == 403
    assert post(client, setup, "admin", "finalize").status_code == 200
    assert post(client, setup, "nurse", "reopen").status_code == 403
    assert post(client, setup, "admin", "reopen").status_code == 200


def test_a_finalized_plan_is_locked_until_it_is_reopened(client, setup):
    post(client, setup, "nurse", "review")
    post(client, setup, "provider", "finalize")

    assert (
        adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats").status_code
        == 409
    )
    assert call(client, "post", f"{plan_url(setup.plan)}notes/", {"text": "x"}, **setup.h("nurse")).status_code == 409
    assert (
        call(client, "post", f"{plan_url(setup.plan)}medications/", {"text": "x"}, **setup.h("provider")).status_code
        == 409
    )

    post(client, setup, "provider", "reopen")
    assert (
        adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats").status_code
        == 201
    )


def test_editing_a_reviewed_plan_sends_it_back_to_in_progress(client, setup):
    post(client, setup, "nurse", "review")

    response = call(client, "post", f"{plan_url(setup.plan)}notes/", {"text": "Changed my mind"}, **setup.h("nurse"))

    body = response.json()
    assert body["status"] == "in_progress"
    assert body["reviewed_by"] is None


def test_new_generated_content_reopens_a_reviewed_plan(client, setup, add_reading):
    post(client, setup, "nurse", "review")

    call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["peanut"]},
        **setup.h("nurse"),
    )  # new content nobody has reviewed

    assert call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()["status"] == "in_progress"


# -- allergies and conditions ------------------------------------------------


def test_editing_allergies_writes_to_the_patient_record_and_regenerates_the_plan(client, setup, fake_model):
    before = fake_model.count

    response = call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["Oats"], "dietary_preference": "vegetarian", "diabetes": "yes"},
        **setup.h("nurse"),
    )

    assert response.status_code == 200
    setup.patient.refresh_from_db()
    assert setup.patient.food_allergies == ["Oats"]
    assert setup.patient.dietary_preference == "vegetarian"
    setup.patient.current_pregnancy.refresh_from_db()
    assert setup.patient.current_pregnancy.diabetes == "yes"
    assert fake_model.count == before + 1  # structural change -> straight away
    body = response.json()
    assert body["allergies_and_conditions"]["food_allergies"] == ["Oats"]
    assert {"field": "diabetes", "label": "Diabetes"} in body["allergies_and_conditions"]["conditions"]
    assert "oats" not in [m["item_key"] for m in body["nutrition"]["content"]["meals"]]


def test_allergies_reach_the_patient_detail_endpoint_too(client, setup):
    call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["peanut"]},
        **setup.h("nurse"),
    )
    detail = call(client, "get", f"/api/patients/{setup.patient.id}/", **setup.h("nurse")).json()
    assert detail["food_allergies"] == ["peanut"]
    assert detail["dietary_preference"] == "none"


def test_an_empty_or_invalid_allergy_edit_is_rejected(client, setup):
    url = f"{plan_url(setup.plan)}allergies-conditions/"
    assert call(client, "patch", url, {}, **setup.h("nurse")).status_code == 400
    assert call(client, "patch", url, {"dietary_preference": "carnivore"}, **setup.h("nurse")).status_code == 400
    assert call(client, "patch", url, {"diabetes": "maybe"}, **setup.h("nurse")).status_code == 400


def test_allergies_can_be_given_when_a_patient_is_onboarded(make_hospital, make_patient):
    patient = make_patient(
        make_hospital("Onboard Allergy Hospital"), food_allergies=["egg"], dietary_preference="vegan"
    )
    assert patient.food_allergies == ["egg"]
    assert patient.dietary_preference == "vegan"


# -- nutrition and exercise as separate responses -----------------------------


def test_nutrition_and_exercise_each_have_their_own_current_plan_endpoint(client, setup):
    base = f"/api/pregnancies/{setup.patient.current_pregnancy.id}/current-care-plan/"
    nutrition = call(client, "get", f"{base}nutrition/", **setup.h("nurse")).json()["care_plan"]
    exercise = call(client, "get", f"{base}exercise/", **setup.h("nurse")).json()["care_plan"]

    assert nutrition["section"] == "nutrition"
    assert "meals" in nutrition["content"]
    assert "activities" not in nutrition["content"]
    assert nutrition["sources"] == [SOURCE_NAME]
    assert nutrition["basis"] == "web_search"
    assert nutrition["generated_at"]
    assert nutrition["month_number"] == 5

    assert exercise["section"] == "exercise"
    assert "activities" in exercise["content"]
    assert "meals" not in exercise["content"]
    assert exercise["sources"] == [SOURCE_NAME]
    assert exercise["generated_at"]


def test_a_sections_content_includes_the_doctors_edits_and_keeps_its_sources(client, setup):
    adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")
    url = f"/api/pregnancies/{setup.patient.current_pregnancy.id}/current-care-plan/nutrition/"
    nutrition = call(client, "get", url, **setup.h("nurse")).json()["care_plan"]
    assert [m["item_key"] for m in nutrition["content"]["meals"]] == ["lentils"]
    assert nutrition["sources"]  # the AI's sources are not edited away by a staff edit


def test_the_patient_gets_each_section_as_its_own_response(client, setup, patient_user, auth):
    mother = patient_user(setup.patient)
    headers = auth(mother.email, mother.password)
    pregnancy = setup.patient.current_pregnancy

    nutrition = call(client, "get", f"/api/pregnancies/{pregnancy.id}/current-care-plan/nutrition/", **headers).json()
    exercise = call(client, "get", f"/api/pregnancies/{pregnancy.id}/current-care-plan/exercise/", **headers).json()

    assert nutrition["care_plan"]["section"] == "nutrition"
    assert nutrition["care_plan"]["sources"]
    assert "clinical_review" not in nutrition["care_plan"]
    assert exercise["care_plan"]["section"] == "exercise"
    assert exercise["care_plan"]["generated_at"]


def test_a_patient_cannot_reach_another_patients_section(client, setup, patient_user, make_patient, add_reading, auth):
    mother = patient_user(setup.patient)
    headers = auth(mother.email, mother.password)
    stranger = make_patient(setup.hospital, first_name="Stranger Two")
    add_reading(stranger, MEDIUM)

    other = f"/api/pregnancies/{stranger.current_pregnancy.id}/current-care-plan/nutrition/"
    assert call(client, "get", other, **headers).status_code == 404


def test_another_hospital_gets_404_on_the_section_endpoints(client, setup, outsider):
    pregnancy = setup.patient.current_pregnancy
    for url in (
        f"/api/pregnancies/{pregnancy.id}/current-care-plan/nutrition/",
        f"/api/pregnancies/{pregnancy.id}/current-care-plan/exercise/",
    ):
        assert call(client, "get", url, **outsider).status_code == 404, url


def test_a_section_with_no_plan_yet_is_null_not_an_error(client, make_hospital, make_patient, make_staff, auth):
    hospital = make_hospital("No Section Hospital")
    patient = make_patient(hospital)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "n@nosection.test")
    url = f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/exercise/"
    body = call(client, "get", url, **auth(nurse.email)).json()
    assert body["care_plan"] is None and body["preparing"] is False


def test_a_nutrition_edit_goes_to_the_nutrition_request_only(client, setup, add_reading, fake_model):
    adjust(client, setup, section="nutrition", list_name="meals", action="remove", item_key="oats")

    call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["peanut"]},
        **setup.h("nurse"),
    )  # re-plans both sections

    assert "removed oats from meals" in fake_model.calls_for("nutrition")[-1]
    assert "<this_patients_doctor_edits_data>" not in fake_model.calls_for("exercise")[-1]


def test_an_exercise_edit_goes_to_the_exercise_request_only(client, setup, add_reading, fake_model):
    adjust(client, setup, section="exercise", list_name="activities", action="remove", item_key="walking")

    call(
        client,
        "patch",
        f"{plan_url(setup.plan)}allergies-conditions/",
        {"food_allergies": ["peanut"]},
        **setup.h("nurse"),
    )

    assert "removed walking from activities" in fake_model.calls_for("exercise")[-1]
    assert "<this_patients_doctor_edits_data>" not in fake_model.calls_for("nutrition")[-1]


def test_there_are_no_by_id_section_endpoints(client, setup):
    # Nutrition and exercise are read through the whole plan, or through the current-plan section endpoints.
    for section in ("nutrition", "exercise"):
        assert call(client, "get", f"{plan_url(setup.plan)}{section}/", **setup.h("nurse")).status_code == 404


# -- the weekly plan's extra output: progress, reading advice, weeks ---------


def test_the_plan_returns_the_weeks_progress_summary(client, setup):
    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()

    assert body["progress"]["facts"]["week_number"] == 20
    assert body["progress"]["facts"]["based_on"] == "last_reading"
    assert "Here is your nutrition and exercise plan for week 20." in body["progress"]["text"]
    assert body["reading_advice"] is None  # nothing has been worse than the plan yet


def test_a_worse_reading_adds_reading_advice_to_the_plan_without_changing_it(client, setup, add_reading, fake_model):
    add_reading(setup.patient, LOW)
    add_reading(setup.patient, HIGH)

    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()

    advice = body["reading_advice"]
    assert advice["risk_level"] == "high"
    assert advice["tips"]
    assert advice["contact_care_team"] is True
    assert advice["contact_message"] == "Please contact your care team today."
    assert advice["sources"] == [SOURCE_NAME]  # the weekly plan's own sources
    assert advice["basis"] == "web_search"
    assert advice["sources_verified"] is False
    assert advice["created_at"]
    assert advice["for_reading_at"]
    assert body["week"]["replans"] == 0  # a quick tip, not a new plan


def test_the_plan_lists_its_weeks_with_their_trend(client, setup):
    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()
    assert [w["week_number"] for w in body["weeks"]] == [20]
    assert body["weeks"][0]["trend"] in (None, "improved", "steady", "worse")


def test_the_patient_sees_the_progress_and_the_advice_too(client, setup, patient_user, add_reading, auth):
    add_reading(setup.patient, LOW)
    add_reading(setup.patient, HIGH)
    mother = patient_user(setup.patient)
    url = f"/api/pregnancies/{setup.patient.current_pregnancy.id}/current-care-plan/"

    plan = call(client, "get", url, **auth(mother.email, mother.password)).json()["care_plan"]

    assert "Areas to work on this week" in plan["progress"]["text"]
    assert plan["reading_advice"]["contact_message"]
    assert "clinical_review" not in plan


def test_the_current_plan_is_the_one_holding_the_week_she_is_in(client, setup):
    url = f"/api/pregnancies/{setup.patient.current_pregnancy.id}/current-care-plan/"
    body = call(client, "get", url, **setup.h("nurse")).json()["care_plan"]
    assert body["id"] == str(setup.plan.id)
    assert body["week"]["week_number"] == 20


def test_quick_advice_stops_showing_once_the_week_has_been_re_planned(client, setup, add_reading, fake_model):
    # The fixture's own reading is "now"; later readings are placed after it (a reading is scored
    # as the pregnancy's latest by its recorded time, so they must really be later).
    add_reading(setup.patient, LOW, minutes_ago=-10)
    add_reading(setup.patient, HIGH, minutes_ago=-310)  # worse than the plan: quick advice
    url = plan_url(setup.plan)
    assert call(client, "get", url, **setup.h("nurse")).json()["reading_advice"] is not None

    add_reading(setup.patient, HIGH, minutes_ago=-460)  # still worse (2 in a row)
    add_reading(setup.patient, HIGH, minutes_ago=-610)  # worse for the third reading in a row: the week is re-planned

    body = call(client, "get", url, **setup.h("nurse")).json()
    assert body["week"]["replans"] == 1
    assert body["reading_advice"] is None


# -- a high-risk patient keeps seeing "contact your care team" -------------------


def test_a_patient_whose_latest_reading_is_high_always_carries_the_contact_message(
    client, make_hospital, make_staff, make_patient, add_reading, fake_model, auth
):
    hospital = make_hospital("Stay High Hospital")
    patient = make_patient(hospital)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@stayhigh.test")
    headers = auth(nurse.email)
    add_reading(patient, HIGH)  # the week's plan is written AT high risk ...
    add_reading(patient, HIGH, minutes_ago=-5)  # ... so a second identical reading earns no new tip
    assert fake_model.advice_count == 0

    body = call(client, "get", f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/", **headers).json()

    plan = body["care_plan"]
    assert plan["reading_advice"] is None
    assert plan["contact_care_team"] is True
    assert plan["contact_message"] == "Please contact your care team today."


def test_the_contact_message_goes_away_when_the_latest_reading_is_no_longer_high(
    client, make_hospital, make_staff, make_patient, add_reading, fake_model, auth
):
    hospital = make_hospital("Back To Low Hospital")
    patient = make_patient(hospital)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@backtolow.test")
    headers = auth(nurse.email)
    add_reading(patient, HIGH)
    add_reading(patient, LOW, minutes_ago=-5)

    plan = call(
        client, "get", f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/", **headers
    ).json()["care_plan"]

    assert plan["contact_care_team"] is False
    assert plan["contact_message"] is None


# -- no official document found: the plan says it was generated by AI -------------


def test_a_plan_with_no_official_source_carries_a_generated_by_ai_notice_for_staff_and_patient(
    client, make_hospital, make_staff, make_patient, add_reading, fake_model, patient_user
):
    fake_model.citations = []  # the live search found nothing official
    hospital = make_hospital("No Official Hospital")
    patient = make_patient(hospital)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@noofficial.test")
    mother = patient_user(patient)
    add_reading(patient, MEDIUM)
    base = f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/"

    for who, password in ((nurse.email, "TestPass!2026"), (mother.email, mother.password)):
        login = client.post("/api/auth/login/", data={"email": who, "password": password}).json()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {login['access']}"}
        plan = call(client, "get", base, **headers).json()["care_plan"]
        for section in ("nutrition", "exercise"):
            assert plan[section]["basis"] == "ai_only"
            assert plan[section]["source_links"] == []
            assert "generated by AI" in plan[section]["generated_by_ai_notice"]
            assert "generated by AI" in plan[section]["sources"][0]
        one = call(client, "get", f"{base}nutrition/", **headers).json()["care_plan"]
        assert "generated by AI" in one["generated_by_ai_notice"]


def test_quick_advice_inherits_the_generated_by_ai_notice_from_its_plan(
    client, make_hospital, make_staff, make_patient, add_reading, fake_model, auth
):
    fake_model.citations = []
    hospital = make_hospital("Advice No Official Hospital")
    patient = make_patient(hospital)
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@advnoofficial.test")
    add_reading(patient, LOW)
    add_reading(patient, HIGH, minutes_ago=-5)

    plan = call(
        client,
        "get",
        f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/",
        **auth(nurse.email),
    ).json()["care_plan"]

    assert plan["reading_advice"]["basis"] == "ai_only"
    assert "generated by AI" in plan["reading_advice"]["generated_by_ai_notice"]


# -- each section carries its own progress, no extra call needed -------------------


def test_each_section_comes_with_its_own_progress_in_the_plan_and_in_its_own_endpoint(client, setup):
    body = call(client, "get", plan_url(setup.plan), **setup.h("nurse")).json()
    assert body["nutrition"]["progress"]["facts"]["section"] == "nutrition"
    assert body["exercise"]["progress"]["facts"]["section"] == "exercise"
    assert body["nutrition"]["progress"]["text"] != body["exercise"]["progress"]["text"]
    assert body["progress"]["text"]  # the overall summary is still there

    base = f"/api/pregnancies/{setup.patient.current_pregnancy.id}/current-care-plan/"
    nutrition = call(client, "get", f"{base}nutrition/", **setup.h("nurse")).json()["care_plan"]
    exercise = call(client, "get", f"{base}exercise/", **setup.h("nurse")).json()["care_plan"]
    assert nutrition["progress"] == body["nutrition"]["progress"]
    assert exercise["progress"] == body["exercise"]["progress"]
    assert nutrition["progress"]["facts"]["axes"] == ["hemoglobin", "glucose", "bp"]
    assert exercise["progress"]["facts"]["axes"] == ["heart_rate", "activity", "stress", "bp"]
