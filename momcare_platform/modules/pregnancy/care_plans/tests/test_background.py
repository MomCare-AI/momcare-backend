"""Plans are written at the same time (nutrition + exercise) and by a background worker.

A reading that needs a plan must save at once; the plan appears a few seconds later and, until
then, the API says "your plan is being prepared". Celery runs eagerly in tests, so a queued
task runs when the test executes the commit callbacks -- which is exactly what a worker does
after the request has been answered.
"""

import threading
from unittest.mock import patch

import pytest
from django.conf import settings as django_settings

from momcare_platform.modules.pregnancy.care_plans.models import CarePlan
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import (
    HIGH,
    LOW,
    MEDIUM,
    current_plan,
    plan_json,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def background(settings):
    settings.CARE_PLAN_GENERATE_IN_BACKGROUND = True


def current_url(patient):
    return f"/api/pregnancies/{patient.current_pregnancy.id}/current-care-plan/"


@pytest.fixture
def nurse_headers(make_staff, auth):
    def _make(hospital, email="nurse@bg.test"):
        return auth(make_staff(hospital.org, django_settings.ROLE_NURSE, email).email)

    return _make


# -- nutrition and exercise are requested at the same time ---------------------------


def test_the_two_sections_are_requested_at_the_same_time(make_hospital, make_patient, add_reading, fake_model):
    """Each request waits for the other to be in flight. Sent one after the other, the
    first would wait until the barrier times out and no plan would be written."""
    barrier = threading.Barrier(2, timeout=5)
    good = plan_json()

    def reply(prompt):
        barrier.wait()
        return good

    fake_model.reply = reply
    patient = make_patient(make_hospital("Parallel Hospital"))

    add_reading(patient, MEDIUM)

    plan = current_plan(patient)
    assert plan is not None
    assert plan.versions.count() == 2
    assert fake_model.count == 1 and len(fake_model.calls_for("exercise")) == 1
    assert not barrier.broken


def test_a_failure_in_one_section_does_not_stop_the_other_from_being_written(
    make_hospital, make_patient, add_reading, fake_model
):
    fake_model.reply_for["nutrition"] = "not json"
    patient = make_patient(make_hospital("Parallel Failure Hospital"))

    add_reading(patient, MEDIUM)

    versions = {v.section: v for v in current_plan(patient).versions.all()}
    assert versions["nutrition"].is_fallback is True
    assert versions["exercise"].is_fallback is False


# -- a background worker writes the plan -----------------------------------------


def test_the_reading_saves_first_and_the_plan_is_written_afterwards_by_the_worker(
    background,
    client,
    make_hospital,
    make_patient,
    add_reading,
    fake_model,
    nurse_headers,
    django_capture_on_commit_callbacks,
):
    hospital = make_hospital("Background Hospital")
    patient = make_patient(hospital)
    headers = nurse_headers(hospital)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        reading = add_reading(patient, MEDIUM)

    assert reading.pk is not None  # saved
    assert fake_model.count == 0  # the model has not been asked
    assert current_plan(patient) is None
    body = client.get(current_url(patient), **headers).json()
    assert body["care_plan"] is None
    assert body["preparing"] is True
    assert "being prepared" in body["status_message"]

    for callback in callbacks:  # the worker picks the task up
        callback()

    assert fake_model.count == 1
    body = client.get(current_url(patient), **headers).json()
    assert body["care_plan"]["nutrition"]["content"]["meals"]
    assert body["preparing"] is False and body["update_pending"] is False and body["status_message"] is None


def test_while_a_plan_exists_and_a_new_reading_is_waiting_the_screen_says_it_is_being_updated(
    background,
    client,
    make_hospital,
    make_patient,
    add_reading,
    fake_model,
    nurse_headers,
    django_capture_on_commit_callbacks,
):
    hospital = make_hospital("Update Pending Hospital")
    patient = make_patient(hospital)
    headers = nurse_headers(hospital)
    with django_capture_on_commit_callbacks(execute=True):
        add_reading(patient, LOW)

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        add_reading(patient, HIGH, minutes_ago=-5)

    body = client.get(current_url(patient), **headers).json()
    assert body["care_plan"] is not None  # the plan she has is still shown ...
    assert body["update_pending"] is True  # ... and she is told it is being updated
    assert "being updated" in body["status_message"]
    assert fake_model.advice_count == 0

    for callback in callbacks:
        callback()

    body = client.get(current_url(patient), **headers).json()
    assert body["update_pending"] is False
    assert fake_model.advice_count == 1
    assert body["care_plan"]["reading_advice"] is not None


def test_a_patient_with_no_readings_is_never_told_a_plan_is_being_prepared(
    background, client, make_hospital, make_patient, nurse_headers
):
    hospital = make_hospital("No Readings Background Hospital")
    patient = make_patient(hospital)

    body = client.get(current_url(patient), **nurse_headers(hospital)).json()

    assert body == {"care_plan": None, "preparing": False, "update_pending": False, "status_message": None}


def test_one_task_is_queued_per_reading_and_only_after_the_commit(
    background, make_hospital, make_patient, add_reading, fake_model, django_capture_on_commit_callbacks
):
    patient = make_patient(make_hospital("Commit Hospital"))

    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        add_reading(patient, MEDIUM)

    assert len(callbacks) == 1
    assert fake_model.count == 0


def test_if_the_queue_is_down_the_plan_is_written_straight_away_instead_of_being_lost(
    background, make_hospital, make_patient, add_reading, fake_model, django_capture_on_commit_callbacks
):
    patient = make_patient(make_hospital("Queue Down Hospital"))

    with patch(
        "momcare_platform.modules.pregnancy.care_plans.tasks.process_assessment_task.delay",
        side_effect=ConnectionError("redis is down"),
    ):
        with django_capture_on_commit_callbacks(execute=True):
            add_reading(patient, MEDIUM)

    assert fake_model.count == 1
    assert CarePlan.objects.filter(pregnancy=patient.current_pregnancy).count() == 1


def test_a_failing_background_plan_never_breaks_the_reading(
    background, make_hospital, make_patient, add_reading, fake_model, django_capture_on_commit_callbacks
):
    from momcare_platform.modules.pregnancy.care_plans import services  # noqa: PLC0415

    patient = make_patient(make_hospital("Worker Crash Hospital"))

    with patch.object(services, "run_section_request", side_effect=RuntimeError("boom")):
        with django_capture_on_commit_callbacks(execute=True):
            reading = add_reading(patient, MEDIUM)

    assert reading.pk is not None
    assert current_plan(patient) is None  # the sweep creates it on its next run


def test_with_the_setting_off_the_plan_is_written_inside_the_request(
    settings, make_hospital, make_patient, add_reading, fake_model
):
    settings.CARE_PLAN_GENERATE_IN_BACKGROUND = False
    patient = make_patient(make_hospital("Inline Hospital"))

    add_reading(patient, MEDIUM)

    assert fake_model.count == 1
    assert current_plan(patient) is not None
