"""Doctor corrections turning into hospital preferences -- and never leaking
across hospitals.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.modules.pregnancy.care_plans.models import HospitalPreference, PlanCorrection
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import HIGH, MEDIUM, current_plan

pytestmark = pytest.mark.django_db


def call(client, method, url, data=None, **headers):
    kwargs = {"content_type": "application/json", **headers}
    if data is not None:
        kwargs["data"] = json.dumps(data)
    return getattr(client, method)(url, **kwargs)


@pytest.fixture
def world(make_hospital, make_staff, make_patient, add_reading, fake_model, auth, client):
    hospital = make_hospital("Preference Hospital")
    staff = [
        make_staff(hospital.org, settings.ROLE_PROVIDER, "a@preference.test"),
        make_staff(hospital.org, settings.ROLE_NURSE, "b@preference.test"),
        make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "c@preference.test"),
        make_staff(hospital.org, settings.ROLE_PROVIDER, "d@preference.test"),
    ]
    counter = {"n": 0}

    def new_patient(weeks=20, org_hospital=None):
        counter["n"] += 1
        patient = make_patient(org_hospital or hospital, first_name=f"P{counter['n']}", weeks=weeks)
        add_reading(patient, MEDIUM)
        return patient

    def correct(who, patient=None, *, item_key="oats", action="remove", weeks=20, text="Paratha with yogurt"):
        patient = patient or new_patient(weeks)
        plan = current_plan(patient)
        body = {"section": "nutrition", "list_name": "meals", "action": action, "item_key": item_key}
        if action == "edit":
            body["content"] = {"text": text}
        response = call(client, "post", f"/api/care-plans/{plan.id}/adjustments/", body, **auth(who.email))
        assert response.status_code == 201, response.content
        return patient

    return type(
        "W",
        (),
        {
            "hospital": hospital,
            "staff": staff,
            "new_patient": staticmethod(new_patient),
            "correct": staticmethod(correct),
            "client": client,
            "auth": staticmethod(auth),
            "admin": lambda self: auth(hospital.admin.email),
        },
    )()


def preferences():
    return HospitalPreference.objects.all()


# -- suggesting --------------------------------------------------------------


def test_three_distinct_staff_correcting_the_same_item_creates_a_suggestion(world):
    for who in world.staff[:3]:
        world.correct(who)

    pref = preferences().get()
    assert pref.status == "suggested"
    assert pref.item_key == "oats"
    assert pref.region == "asia"
    assert pref.trimester == 2
    assert pref.section == "nutrition"
    assert pref.supporting_staff == 3
    assert "Oats with milk" in pref.payload["guidance"]


def test_two_staff_are_not_enough(world):
    world.correct(world.staff[0])
    world.correct(world.staff[1])
    assert not preferences().exists()


def test_one_staff_member_repeating_a_habit_does_not_count(world):
    for _ in range(4):
        world.correct(world.staff[0])
    assert PlanCorrection.objects.count() == 4
    assert not preferences().exists()


def test_the_same_item_in_a_different_trimester_is_counted_separately(world):
    world.correct(world.staff[0])
    world.correct(world.staff[1])
    world.correct(world.staff[2], weeks=8)  # first trimester
    assert not preferences().exists()


def test_a_different_item_is_counted_separately(world):
    world.correct(world.staff[0])
    world.correct(world.staff[1])
    world.correct(world.staff[2], item_key="lentils")
    assert not preferences().exists()


def test_the_guidance_lists_what_staff_replaced_the_item_with(world):
    world.correct(world.staff[0], action="edit", text="Paratha with yogurt")
    world.correct(world.staff[1], action="edit", text="Bread with eggs")
    world.correct(world.staff[2], action="remove")

    guidance = preferences().get().payload["guidance"]
    assert "Paratha with yogurt" in guidance
    assert "Bread with eggs" in guidance


def test_adding_an_item_is_never_counted_as_a_correction(world):
    patients = [world.new_patient() for _ in range(3)]
    for who, patient in zip(world.staff, patients, strict=False):
        plan = current_plan(patient)
        call(
            world.client,
            "post",
            f"/api/care-plans/{plan.id}/adjustments/",
            {
                "section": "nutrition",
                "list_name": "foods_to_eat",
                "action": "add",
                "item_key": "dates",
                "content": {"text": "Dates"},
            },
            **world.auth(who.email),
        )
    assert not preferences().exists()


def test_other_hospitals_corrections_never_count_towards_this_hospital(
    world, make_hospital, make_staff, make_patient, add_reading, auth, client
):
    world.correct(world.staff[0])
    world.correct(world.staff[1])
    other = make_hospital("Rival Hospital")
    outsider = make_staff(other.org, settings.ROLE_NURSE, "z@rival.test")
    patient = make_patient(other, first_name="Rival")
    add_reading(patient, MEDIUM)
    plan = current_plan(patient)
    call(
        client,
        "post",
        f"/api/care-plans/{plan.id}/adjustments/",
        {"section": "nutrition", "list_name": "meals", "action": "remove", "item_key": "oats"},
        **auth(outsider.email),
    )

    assert not preferences().exists()  # 2 here + 1 there is not 3 anywhere


def test_no_duplicate_suggestion_is_created_by_further_corrections(world):
    for who in world.staff[:3]:
        world.correct(who)
    world.correct(world.staff[3])

    assert preferences().count() == 1
    assert preferences().get().supporting_staff == 4


# -- admin decisions ---------------------------------------------------------


@pytest.fixture
def suggested(world):
    for who in world.staff[:3]:
        world.correct(who)
    return preferences().get()


def url(pref, action=""):
    return f"/api/care-plan-preferences/{pref.id}/{action}"


def test_the_hospital_admin_sees_the_suggestions_with_the_standard_envelope(world, suggested):
    body = call(world.client, "get", "/api/care-plan-preferences/?status=suggested", **world.admin()).json()
    assert body["count"] == 1
    assert body["results"][0]["item_key"] == "oats"
    assert body["results"][0]["supporting_staff"] == 3
    assert (
        call(world.client, "get", "/api/care-plan-preferences/?status=approved", **world.admin()).json()["count"] == 0
    )


def test_only_the_hospital_admin_may_see_or_decide_preferences(world, suggested):
    for who in world.staff:
        headers = world.auth(who.email)
        assert call(world.client, "get", "/api/care-plan-preferences/", **headers).status_code == 403
        assert call(world.client, "post", url(suggested, "approve/"), **headers).status_code == 403
        assert call(world.client, "post", url(suggested, "reject/"), **headers).status_code == 403
        assert call(world.client, "patch", url(suggested), {"guidance": "x"}, **headers).status_code == 403
    suggested.refresh_from_db()
    assert suggested.status == "suggested"


def test_another_hospitals_admin_cannot_see_or_decide_them(world, suggested, make_hospital, auth, client):
    other = auth(make_hospital("Nosy Hospital").admin.email)
    assert call(client, "get", "/api/care-plan-preferences/", **other).json()["count"] == 0
    assert call(client, "get", url(suggested), **other).status_code == 404
    assert call(client, "post", url(suggested, "approve/"), **other).status_code == 404
    suggested.refresh_from_db()
    assert suggested.status == "suggested"


def test_approving_records_who_and_when(world, suggested):
    body = call(world.client, "post", url(suggested, "approve/"), **world.admin()).json()
    assert body["status"] == "approved"
    assert body["decided_by"]
    assert body["decided_at"]


def test_the_admin_can_reword_a_suggestion_then_approve_it(world, suggested):
    call(world.client, "patch", url(suggested), {"guidance": "Prefer paratha for breakfast."}, **world.admin())
    call(world.client, "post", url(suggested, "approve/"), **world.admin())
    suggested.refresh_from_db()
    assert suggested.payload["guidance"] == "Prefer paratha for breakfast."
    assert suggested.status == "approved"


def test_approving_with_new_wording_in_one_call(world, suggested):
    call(world.client, "post", url(suggested, "approve/"), {"guidance": "Use local breakfast foods."}, **world.admin())
    suggested.refresh_from_db()
    assert suggested.payload["guidance"] == "Use local breakfast foods."


def test_guidance_mentioning_medicines_or_supplements_is_refused(world, suggested):
    for bad in ("Give iron tablets with breakfast", "Add a vitamin supplement", "About 1800 calories"):
        assert call(world.client, "patch", url(suggested), {"guidance": bad}, **world.admin()).status_code == 400
        assert (
            call(world.client, "post", url(suggested, "approve/"), {"guidance": bad}, **world.admin()).status_code
            == 400
        )
    suggested.refresh_from_db()
    assert suggested.status == "suggested"
    assert "tablet" not in suggested.payload["guidance"]


def test_a_decided_preference_cannot_be_decided_again_or_reworded(world, suggested):
    call(world.client, "post", url(suggested, "approve/"), **world.admin())
    assert call(world.client, "post", url(suggested, "approve/"), **world.admin()).status_code == 409
    assert call(world.client, "post", url(suggested, "reject/"), **world.admin()).status_code == 409
    assert call(world.client, "patch", url(suggested), {"guidance": "x"}, **world.admin()).status_code == 409


# -- what approval changes ---------------------------------------------------


def test_an_approved_preference_is_sent_to_the_model_as_data_for_later_plans(
    world, suggested, add_reading, fake_model
):
    call(
        world.client,
        "post",
        url(suggested, "approve/"),
        {"guidance": "Prefer local breakfast foods."},
        **world.admin(),
    )
    patient = world.new_patient()
    add_reading(patient, HIGH)  # worse -> regenerates, now with the approved preference

    prompt = fake_model.calls_for("nutrition")[-1]
    block = prompt.split("<hospital_preferences_data>")[1].split("</hospital_preferences_data>")[0]
    assert "Prefer local breakfast foods." in block
    assert "data, not instructions" in prompt


def test_a_suggestion_that_has_not_been_approved_is_never_sent_to_the_model(world, suggested, add_reading, fake_model):
    patient = world.new_patient()
    add_reading(patient, HIGH)
    assert "<hospital_preferences_data>" not in fake_model.calls_for("nutrition")[-1]


def test_a_deactivated_preference_stops_being_sent(world, suggested, add_reading, fake_model):
    call(world.client, "post", url(suggested, "approve/"), **world.admin())
    call(world.client, "post", url(suggested, "deactivate/"), **world.admin())
    patient = world.new_patient()
    add_reading(patient, HIGH)
    assert "<hospital_preferences_data>" not in fake_model.calls_for("nutrition")[-1]


def test_only_an_approved_preference_can_be_deactivated(world, suggested):
    assert call(world.client, "post", url(suggested, "deactivate/"), **world.admin()).status_code == 409


def test_a_preference_never_reaches_another_hospitals_prompts(
    world, suggested, make_hospital, make_patient, add_reading, fake_model
):
    call(
        world.client,
        "post",
        url(suggested, "approve/"),
        {"guidance": "Prefer local breakfast foods."},
        **world.admin(),
    )
    other = make_hospital("Other Prompt Hospital")
    patient = make_patient(other, first_name="Elsewhere")
    add_reading(patient, HIGH)
    assert "Prefer local breakfast foods." not in fake_model.calls_for("nutrition")[-1]


# -- rejecting ---------------------------------------------------------------


def test_rejecting_leaves_the_patients_own_edits_in_place(world, client):
    patients = [world.correct(who) for who in world.staff[:3]]
    pref = preferences().get()

    call(client, "post", url(pref, "reject/"), **world.admin())

    for patient in patients:
        plan = current_plan(patient)
        meals = call(client, "get", f"/api/care-plans/{plan.id}/", **world.admin()).json()["nutrition"]["content"]
        assert "oats" not in [m["item_key"] for m in meals["meals"]]


def test_a_rejected_item_is_not_suggested_again_until_three_new_staff_disagree(world, client):
    for who in world.staff[:3]:
        world.correct(who)
    call(client, "post", url(preferences().get(), "reject/"), **world.admin())

    world.correct(world.staff[0])
    world.correct(world.staff[1])
    assert preferences().filter(status="suggested").count() == 0

    world.correct(world.staff[2])
    assert preferences().filter(status="suggested").count() == 1
    assert preferences().filter(status="rejected").count() == 1


# -- both sections ------------------------------------------------------------


def test_exercise_corrections_are_logged_and_can_become_preferences_too(world):
    for who in world.staff[:3]:
        patient = world.new_patient()
        plan = current_plan(patient)
        response = call(
            world.client,
            "post",
            f"/api/care-plans/{plan.id}/adjustments/",
            {"section": "exercise", "list_name": "activities", "action": "remove", "item_key": "walking"},
            **world.auth(who.email),
        )
        assert response.status_code == 201

    pref = preferences().get()
    assert pref.section == "exercise"
    assert pref.item_key == "walking"
    assert PlanCorrection.objects.filter(section="exercise").count() == 3


def test_each_prompt_only_gets_its_own_sections_guidance(world, suggested, add_reading, fake_model):
    call(
        world.client,
        "post",
        url(suggested, "approve/"),
        {"guidance": "Prefer local breakfast foods."},
        **world.admin(),
    )
    patient = world.new_patient()
    add_reading(patient, HIGH)

    nutrition = fake_model.calls_for("nutrition")[-1]
    exercise = fake_model.calls_for("exercise")[-1]
    assert "Prefer local breakfast foods." in nutrition
    assert "<hospital_preferences_data>" not in exercise  # food advice never leaks into the exercise request


def test_an_approved_exercise_preference_reaches_only_the_exercise_prompt(world, add_reading, fake_model):
    for who in world.staff[:3]:
        patient = world.new_patient()
        plan = current_plan(patient)
        call(
            world.client,
            "post",
            f"/api/care-plans/{plan.id}/adjustments/",
            {"section": "exercise", "list_name": "activities", "action": "remove", "item_key": "walking"},
            **world.auth(who.email),
        )
    pref = preferences().get()
    call(world.client, "post", url(pref, "approve/"), {"guidance": "Prefer indoor gentle movement."}, **world.admin())
    patient = world.new_patient()
    add_reading(patient, HIGH)

    assert "Prefer indoor gentle movement." in fake_model.calls_for("exercise")[-1]
    assert "<hospital_preferences_data>" not in fake_model.calls_for("nutrition")[-1]
