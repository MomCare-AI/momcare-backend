"""What a patient can read of her own care plan, on the mobile app.

Everything is read-only: she sees the plan, nutrition, exercise, the doctor's
medications and notes, and her earlier months' plans -- and can change none of it.
"""

import json

import pytest
from django.conf import settings

from momcare_platform.modules.pregnancy.care_plans.tests.conftest import MEDIUM, current_plan

pytestmark = pytest.mark.django_db

STAFF_ONLY_KEYS = (
    "reviewed_by",
    "reviewed_at",
    "finalized_by",
    "finalized_at",
    "last_evaluated_at",
    "clinical_review",
)


def call(client, method, url, data=None, **headers):
    kwargs = {"content_type": "application/json", **headers}
    if data is not None:
        kwargs["data"] = json.dumps(data)
    return getattr(client, method)(url, **kwargs)


@pytest.fixture
def setup(make_hospital, make_staff, make_patient, add_reading, fake_model, auth, patient_user):
    """One hospital, a patient with a plan, her own app login, and a nurse and a provider."""
    hospital = make_hospital("Patient Reads Hospital")
    patient = make_patient(hospital)
    add_reading(patient, MEDIUM)
    mother = patient_user(patient)
    staff = {
        "provider": make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@patientreads.test"),
        "nurse": make_staff(hospital.org, settings.ROLE_NURSE, "nurse@patientreads.test"),
    }
    plan = current_plan(patient)
    pregnancy = patient.current_pregnancy
    return type(
        "S",
        (),
        {
            "hospital": hospital,
            "patient": patient,
            "plan": plan,
            "pregnancy": pregnancy,
            "mine": auth(mother.email, mother.password),
            "staff": lambda self, who: auth(staff[who].email, "TestPass!2026"),
            "section": lambda self, name: f"/api/pregnancies/{pregnancy.id}/current-care-plan/{name}/",
        },
    )()


def add_medication(client, setup, text="Iron tablet once a day, as prescribed in clinic"):
    url = f"/api/care-plans/{setup.plan.id}/medications/"
    response = call(client, "post", url, {"text": text}, **setup.staff("provider"))
    assert response.status_code in (200, 201), response.content


def add_note(client, setup, text="Please rest more this week."):
    url = f"/api/care-plans/{setup.plan.id}/notes/"
    response = call(client, "post", url, {"text": text}, **setup.staff("nurse"))
    assert response.status_code in (200, 201), response.content


# -- medications and notes as their own responses -----------------------------------


def test_she_gets_her_medications_as_their_own_response(client, setup):
    add_medication(client, setup)

    body = call(client, "get", setup.section("medications"), **setup.mine).json()["care_plan"]

    assert body["section"] == "medications"
    assert [m["text"] for m in body["items"]] == ["Iron tablet once a day, as prescribed in clinic"]
    assert body["items"][0]["id"] and body["items"][0]["created_at"] and body["items"][0]["added_by"]
    assert body["care_plan_id"] == str(setup.plan.id)
    assert body["disclaimer"]
    assert "clinical_review" not in body


def test_she_gets_the_doctors_notes_as_their_own_response(client, setup):
    add_note(client, setup)

    body = call(client, "get", setup.section("notes"), **setup.mine).json()["care_plan"]

    assert body["section"] == "notes"
    assert [n["text"] for n in body["items"]] == ["Please rest more this week."]


def test_the_lists_are_empty_not_missing_when_nothing_was_added(client, setup):
    assert call(client, "get", setup.section("medications"), **setup.mine).json()["care_plan"]["items"] == []
    assert call(client, "get", setup.section("notes"), **setup.mine).json()["care_plan"]["items"] == []


def test_a_removed_medication_or_note_is_not_shown_to_her(client, setup):
    add_medication(client, setup, "Old medicine")
    add_note(client, setup, "Old note")
    med_id = call(client, "get", setup.section("medications"), **setup.mine).json()["care_plan"]["items"][0]["id"]
    note_id = call(client, "get", setup.section("notes"), **setup.mine).json()["care_plan"]["items"][0]["id"]
    base = f"/api/care-plans/{setup.plan.id}"
    assert call(client, "delete", f"{base}/medications/{med_id}/", **setup.staff("provider")).status_code in (200, 204)
    assert call(client, "delete", f"{base}/notes/{note_id}/", **setup.staff("nurse")).status_code in (200, 204)

    assert call(client, "get", setup.section("medications"), **setup.mine).json()["care_plan"]["items"] == []
    assert call(client, "get", setup.section("notes"), **setup.mine).json()["care_plan"]["items"] == []


def test_the_same_items_are_inside_the_full_plan(client, setup):
    add_medication(client, setup)
    add_note(client, setup)

    plan = call(client, "get", setup.section("").rstrip("/") + "/", **setup.mine).json()["care_plan"]

    assert [m["text"] for m in plan["medications"]] == ["Iron tablet once a day, as prescribed in clinic"]
    assert [n["text"] for n in plan["notes"]] == ["Please rest more this week."]


def test_a_woman_with_no_plan_yet_gets_none_not_an_error(client, make_hospital, make_patient, patient_user, auth):
    hospital = make_hospital("No Plan Yet Hospital")
    patient = make_patient(hospital)  # no reading, so no plan
    mother = patient_user(patient, email="noplan@patientreads.test")
    pregnancy = patient.current_pregnancy

    for name in ("medications", "notes"):
        response = call(
            client,
            "get",
            f"/api/pregnancies/{pregnancy.id}/current-care-plan/{name}/",
            **auth(mother.email, mother.password),
        )
        assert response.status_code == 200
        assert response.json()["care_plan"] is None


def test_she_cannot_read_another_patients_medications_or_notes(client, setup, make_patient, add_reading):
    stranger = make_patient(setup.hospital, first_name="Stranger")
    add_reading(stranger, MEDIUM)

    for name in ("medications", "notes"):
        url = f"/api/pregnancies/{stranger.current_pregnancy.id}/current-care-plan/{name}/"
        assert call(client, "get", url, **setup.mine).status_code == 404, name


def test_another_hospital_gets_404_on_medications_and_notes(client, setup, make_hospital, auth):
    rival = make_hospital("Rival Reads Hospital")
    for name in ("medications", "notes"):
        assert call(client, "get", setup.section(name), **auth(rival.admin.email)).status_code == 404, name


def test_staff_can_open_the_same_endpoints(client, setup):
    add_medication(client, setup)

    body = call(client, "get", setup.section("medications"), **setup.staff("nurse")).json()["care_plan"]

    assert [m["text"] for m in body["items"]] == ["Iron tablet once a day, as prescribed in clinic"]


def test_she_cannot_write_to_medications_or_notes(client, setup):
    for name in ("medications", "notes"):
        for method in ("post", "patch", "put", "delete"):
            response = call(client, method, setup.section(name), {"text": "x"}, **setup.mine)
            assert response.status_code in (403, 405), f"{method} {name} -> {response.status_code}"


# -- her earlier plans --------------------------------------------------------------


def test_she_can_list_her_own_plans(client, setup):
    add_medication(client, setup)

    body = call(client, "get", "/api/my-care-plans/", **setup.mine).json()

    assert {"count", "page", "page_size", "total_pages", "next", "previous", "results"} <= set(body)
    assert body["count"] == 1
    plan = body["results"][0]
    assert plan["id"] == str(setup.plan.id)
    assert plan["month_number"] == setup.plan.month_number
    assert [m["text"] for m in plan["medications"]] == ["Iron tablet once a day, as prescribed in clinic"]
    assert plan["nutrition"]["content"]["meals"]


def test_her_list_has_no_staff_bookkeeping(client, setup):
    plan = call(client, "get", "/api/my-care-plans/", **setup.mine).json()["results"][0]

    for key in STAFF_ONLY_KEYS:
        assert key not in plan, key


def test_her_list_never_includes_another_womans_plans(client, setup, make_patient, add_reading):
    stranger = make_patient(setup.hospital, first_name="Stranger Plans")
    add_reading(stranger, MEDIUM)

    body = call(client, "get", "/api/my-care-plans/", **setup.mine).json()

    assert body["count"] == 1
    assert body["results"][0]["pregnancy_id"] == str(setup.pregnancy.id)


def test_filtering_her_list_by_someone_elses_pregnancy_gives_nothing(client, setup, make_patient, add_reading):
    stranger = make_patient(setup.hospital, first_name="Stranger Filter")
    add_reading(stranger, MEDIUM)

    body = call(client, "get", f"/api/my-care-plans/?pregnancy={stranger.current_pregnancy.id}", **setup.mine).json()

    assert body["count"] == 0


def test_she_can_open_one_of_her_own_plans(client, setup):
    add_note(client, setup)

    response = call(client, "get", f"/api/my-care-plans/{setup.plan.id}/", **setup.mine)

    assert response.status_code == 200
    plan = response.json()
    assert plan["id"] == str(setup.plan.id)
    assert [n["text"] for n in plan["notes"]] == ["Please rest more this week."]
    for key in STAFF_ONLY_KEYS:
        assert key not in plan, key


def test_she_cannot_open_another_womans_plan(client, setup, make_patient, add_reading):
    stranger = make_patient(setup.hospital, first_name="Stranger Open")
    add_reading(stranger, MEDIUM)
    other = current_plan(stranger)

    assert call(client, "get", f"/api/my-care-plans/{other.id}/", **setup.mine).status_code == 404


def test_a_garbage_plan_id_is_a_clean_404(client, setup):
    assert call(client, "get", "/api/my-care-plans/not-a-uuid/", **setup.mine).status_code == 404


def test_staff_cannot_use_her_endpoints(client, setup):
    assert call(client, "get", "/api/my-care-plans/", **setup.staff("nurse")).status_code == 403
    assert call(client, "get", f"/api/my-care-plans/{setup.plan.id}/", **setup.staff("nurse")).status_code == 403


def test_her_plans_need_a_signed_in_user(client, setup):
    assert call(client, "get", "/api/my-care-plans/").status_code == 401


def test_her_plan_endpoints_are_read_only(client, setup):
    for url in ("/api/my-care-plans/", f"/api/my-care-plans/{setup.plan.id}/"):
        for method in ("post", "patch", "put", "delete"):
            assert call(client, method, url, {"text": "x"}, **setup.mine).status_code == 405, f"{method} {url}"
