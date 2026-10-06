"""The sweep safety net, and the read-only admin."""

from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib import admin
from django.core.management import call_command
from django.test import RequestFactory

from momcare_platform.modules.pregnancy.care_plans import models, services
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import MEDIUM, current_plan, plan_json

pytestmark = pytest.mark.django_db


def sweep() -> str:
    out = StringIO()
    call_command("sweep_care_plans", stdout=out)
    return out.getvalue()


def test_the_sweep_writes_the_plan_for_a_week_the_hook_missed(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Sweep Hospital"))
    with patch.object(services, "run_section_request", side_effect=RuntimeError("the model was down")):
        add_reading(patient, MEDIUM)
    assert not models.CarePlanSectionVersion.objects.exists()

    assert "Started 1" in sweep()

    assert models.CarePlanSectionVersion.objects.filter(care_plan=current_plan(patient)).count() == 2


def test_the_sweep_leaves_a_week_that_already_has_a_plan_alone(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Sweep Skip Hospital"))
    add_reading(patient, MEDIUM)
    calls = fake_model.count

    assert "No care plan needed evaluating" in sweep()
    assert fake_model.count == calls


def test_the_sweep_starts_the_new_weeks_plan_for_a_patient_who_sent_no_readings(
    make_hospital, make_patient, add_reading, fake_model
):
    from datetime import timedelta  # noqa: PLC0415

    from django.utils import timezone  # noqa: PLC0415

    patient = make_patient(make_hospital("Sweep Quiet Hospital"), weeks=20)
    add_reading(patient, MEDIUM)

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=7)):
        assert "Started 1" in sweep()

    assert models.CareWeek.objects.filter(care_plan__pregnancy=patient.current_pregnancy).count() == 2
    assert fake_model.count == 2


def test_the_sweep_ignores_patients_with_no_readings_and_inactive_patients(
    make_hospital, make_patient, add_reading, fake_model
):
    hospital = make_hospital("Sweep Ignore Hospital")
    make_patient(hospital, first_name="Silent")
    inactive = make_patient(hospital, first_name="Inactive")
    add_reading(inactive, MEDIUM)
    models.CarePlan.objects.all().delete()
    inactive.is_active = False
    inactive.save(update_fields=["is_active"])

    assert "No care plan needed evaluating" in sweep()
    assert not models.CarePlan.objects.exists()


@pytest.mark.parametrize(
    "model",
    [
        models.CarePlan,
        models.CarePlanSectionVersion,
        models.CarePlanAdjustment,
        models.CarePlanMedication,
        models.CarePlanNote,
        models.PlanCorrection,
        models.HospitalPreference,
        models.CareWeek,
        models.ReadingAdvice,
    ],
)
def test_no_care_plan_table_can_be_added_changed_or_deleted_in_the_admin(model, admin_user):
    request = RequestFactory().get("/admin/")
    request.user = admin_user
    model_admin = admin.site._registry[model]

    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False


# -- retrying plans stuck on the generic baseline ----------------------------


def newest(plan, section="nutrition"):
    return (
        models.CarePlanSectionVersion.objects.filter(care_plan=plan, section=section).order_by("-created_at").first()
    )


def test_the_sweep_replaces_a_stuck_generic_plan_once_the_model_is_back(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Retry Sweep Hospital"))
    fake_model.reply = None  # the model is down when the reading arrives
    add_reading(patient, MEDIUM)
    plan = current_plan(patient)
    assert newest(plan).is_fallback is True

    fake_model.reply = plan_json()  # ...and back later
    assert "retried 1" in sweep()

    assert newest(plan).is_fallback is False
    assert newest(plan, "exercise").is_fallback is False


def test_a_retry_that_fails_again_does_not_pile_up_identical_generic_versions(
    make_hospital, make_patient, add_reading, fake_model
):
    patient = make_patient(make_hospital("Retry Fail Hospital"))
    fake_model.reply = None
    add_reading(patient, MEDIUM)
    plan = current_plan(patient)
    before = models.CarePlanSectionVersion.objects.filter(care_plan=plan).count()

    sweep()
    sweep()

    assert models.CarePlanSectionVersion.objects.filter(care_plan=plan).count() == before


def test_the_sweep_leaves_a_real_plan_alone(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Retry Skip Hospital"))
    add_reading(patient, MEDIUM)
    calls = fake_model.count

    assert "No care plan needed evaluating" in sweep()
    assert fake_model.count == calls


def test_a_retry_only_redoes_the_section_that_is_on_the_baseline(make_hospital, make_patient, add_reading, fake_model):
    patient = make_patient(make_hospital("Retry One Section Hospital"))
    fake_model.reply_for["exercise"] = None  # only the exercise request fails
    add_reading(patient, MEDIUM)
    plan = current_plan(patient)
    assert newest(plan, "nutrition").is_fallback is False
    assert newest(plan, "exercise").is_fallback is True
    nutrition_calls = len(fake_model.calls_for("nutrition"))
    good_nutrition_id = newest(plan, "nutrition").id

    fake_model.reply_for.clear()  # the model is back
    assert "retried 1" in sweep()

    assert newest(plan, "exercise").is_fallback is False
    assert newest(plan, "nutrition").id == good_nutrition_id  # untouched
    assert len(fake_model.calls_for("nutrition")) == nutrition_calls  # and not even asked again


def test_the_celery_task_runs_the_same_sweep(make_hospital, make_patient, add_reading, fake_model):
    from datetime import timedelta  # noqa: PLC0415

    from django.utils import timezone  # noqa: PLC0415

    from momcare_platform.modules.pregnancy.care_plans.tasks import sweep_task  # noqa: PLC0415

    patient = make_patient(make_hospital("Celery Hospital"), weeks=20)
    add_reading(patient, MEDIUM)

    with patch("django.utils.timezone.localdate", return_value=timezone.localdate() + timedelta(days=7)):
        result = sweep_task()

    assert result == {"started": 1, "retried": 0}
    assert sweep_task.name == "care_plans.sweep"
