"""The two dashboard workflows: plans awaiting review, and patients with no plan."""

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.modules.pregnancy.care_plans.models import CarePlan
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import MEDIUM, current_plan

pytestmark = pytest.mark.django_db


def get(client, url, headers):
    return client.get(url, **headers)


@pytest.fixture
def desk(make_hospital, make_staff, auth, client, fake_model):
    hospital = make_hospital("Workflow Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "n@workflow.test")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "p@workflow.test")
    return type(
        "D", (), {"hospital": hospital, "client": client, "h": auth(nurse.email), "provider_h": auth(provider.email)}
    )()


def ids(response):
    return {row["id"] for row in response.json()["results"]}


def review_list(desk):
    return ids(get(desk.client, "/api/patients/?workflow=care_plan_review", desk.h))


def missing_list(desk):
    return ids(get(desk.client, "/api/patients/?workflow=care_plan_missing", desk.h))


def test_a_patient_with_an_unreviewed_plan_is_listed_for_review(desk, make_patient, add_reading):
    patient = make_patient(desk.hospital)
    add_reading(patient, MEDIUM)

    assert review_list(desk) == {str(patient.id)}
    assert missing_list(desk) == set()


def test_reviewing_the_plan_removes_the_patient_and_reopening_brings_her_back(desk, make_patient, add_reading):
    patient = make_patient(desk.hospital)
    add_reading(patient, MEDIUM)
    plan = current_plan(patient)

    desk.client.post(f"/api/care-plans/{plan.id}/review/", **desk.h)
    assert review_list(desk) == set()

    desk.client.post(f"/api/care-plans/{plan.id}/reopen/", **desk.provider_h)
    assert review_list(desk) == {str(patient.id)}


def test_a_patient_with_no_plan_this_month_is_listed_as_missing(desk, make_patient):
    patient = make_patient(desk.hospital)

    assert missing_list(desk) == {str(patient.id)}
    assert review_list(desk) == set()


def test_a_first_reading_moves_her_from_missing_to_review(desk, make_patient, add_reading):
    patient = make_patient(desk.hospital)
    add_reading(patient, MEDIUM)
    assert missing_list(desk) == set()
    assert review_list(desk) == {str(patient.id)}


def test_only_a_plan_for_the_current_30_day_block_counts(desk, make_patient):
    patient = make_patient(desk.hospital)
    last_year = timezone.localdate() - timedelta(days=365)
    CarePlan.objects.create(
        pregnancy=patient.current_pregnancy,
        month_number=1,
        period_start=last_year,
        period_end=last_year + timedelta(days=29),
    )

    assert missing_list(desk) == {str(patient.id)}  # an old plan does not cover this month
    assert review_list(desk) == set()


def test_a_patient_without_a_pregnancy_is_in_neither_list(desk):
    from momcare_platform.core.patients.services import onboard_patient  # noqa: PLC0415

    patient = onboard_patient(
        organization=desk.hospital.org, patient_data={"first_name": "Nopreg", "last_name": "Bibi"}, pregnancy_data=None
    )
    assert str(patient.id) not in missing_list(desk) | review_list(desk)


def test_the_lists_are_scoped_to_the_callers_hospital(desk, make_hospital, make_patient, add_reading):
    other = make_hospital("Other Workflow Hospital")
    elsewhere = make_patient(other, first_name="Elsewhere")
    add_reading(elsewhere, MEDIUM)
    mine = make_patient(desk.hospital)
    add_reading(mine, MEDIUM)

    assert review_list(desk) == {str(mine.id)}


def test_the_dashboard_counts_always_agree_with_the_lists(desk, make_patient, add_reading):
    reviewed = make_patient(desk.hospital, first_name="Reviewed")
    add_reading(reviewed, MEDIUM)
    waiting = make_patient(desk.hospital, first_name="Waiting")
    add_reading(waiting, MEDIUM)
    make_patient(desk.hospital, first_name="Silent")  # no readings -> missing
    desk.client.post(f"/api/care-plans/{current_plan(reviewed).id}/review/", **desk.h)

    kpis = get(desk.client, "/api/patients/dashboard-kpis/", desk.h).json()["workflow"]

    assert kpis["care_plan_review"] == len(review_list(desk)) == 1
    assert kpis["care_plan_missing"] == len(missing_list(desk)) == 1
    # the existing workflows are untouched
    assert {"risk_review", "low_confidence"} <= set(kpis)


def test_a_low_risk_weekly_plan_does_not_need_a_doctors_review(desk, make_patient, add_reading):
    from momcare_platform.modules.pregnancy.care_plans.tests.conftest import LOW  # noqa: PLC0415

    patient = make_patient(desk.hospital)
    add_reading(patient, LOW)

    assert review_list(desk) == set()
    assert missing_list(desk) == set()  # she does have a plan this week


def test_a_high_risk_weekly_plan_needs_review(desk, make_patient, add_reading):
    from momcare_platform.modules.pregnancy.care_plans.tests.conftest import HIGH  # noqa: PLC0415

    patient = make_patient(desk.hospital)
    add_reading(patient, HIGH)

    assert review_list(desk) == {str(patient.id)}
