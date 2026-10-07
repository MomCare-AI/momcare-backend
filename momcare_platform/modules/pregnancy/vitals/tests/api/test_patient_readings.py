"""What a patient reads of her own readings and risk, on the mobile app.

Strictly read-only: readings are entered by the hospital or a device, never by
her. She sees her reading list (with the usual period filters and statistics),
the latest reading, the 30-day averages and her risk -- and none of the staff
review workflow behind that risk.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.users.models import Role, User
from momcare_platform.modules.pregnancy.care_plans.tests.conftest import HIGH, LOW, MEDIUM
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment, VitalReading

pytestmark = pytest.mark.django_db

# Everything about the staff review of a risk judgement: never shown to her.
STAFF_RISK_KEYS = (
    "risk_level",
    "risk_level_display",
    "previous_risk_level",
    "confirmed_risk_level",
    "review_status",
    "review_status_display",
    "flagged_for_review",
    "confidence",
    "needs_review",
    "verified_at",
    "verified_by_name",
)


def make_patient(hospital, first_name="Ayesha"):
    return onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": first_name, "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=24)},
    )


def link_account(patient, email="mother@vitals.test", password="MotherPass!2026"):
    user = User.objects.create_user(
        email=email,
        password=password,
        first_name="Mother",
        last_name="User",
        role=Role.objects.get(code=settings.ROLE_PATIENT),
    )
    user.organization = patient.organization
    user.is_email_verified = True
    user.save(update_fields=["organization", "is_email_verified", "updated_at"])
    patient.user = user
    patient.save(update_fields=["user", "updated_at"])
    return user


def add_reading(pregnancy, vitals, *, days_ago=0):
    return VitalReading.objects.create(
        pregnancy=pregnancy,
        source=VitalReading.SOURCE_MANUAL,
        recorded_at=timezone.now() - timedelta(days=days_ago, minutes=1),
        **vitals,
    )


@pytest.fixture
def setup(make_hospital, auth):
    """One hospital; a woman with an app login and three readings (today, 3 days ago, 40 days ago)."""
    hospital = make_hospital("Patient Vitals Hospital")
    patient = make_patient(hospital)
    user = link_account(patient)
    pregnancy = patient.current_pregnancy
    add_reading(pregnancy, {**LOW, "systolic_bp": "112", "diastolic_bp": "72"}, days_ago=40)
    add_reading(pregnancy, {**MEDIUM, "systolic_bp": "130", "diastolic_bp": "84"}, days_ago=3)
    add_reading(pregnancy, {**LOW, "systolic_bp": "120", "diastolic_bp": "80"}, days_ago=0)
    return type(
        "S",
        (),
        {
            "hospital": hospital,
            "patient": patient,
            "pregnancy": pregnancy,
            "mine": auth(user.email, "MotherPass!2026"),
            "staff": auth(hospital.admin.email),
            "url": lambda self, name, pregnancy=None: f"/api/pregnancies/{(pregnancy or self.pregnancy).id}/{name}/",
        },
    )()


# -- the reading list -----------------------------------------------------------------


def test_she_sees_her_readings_newest_first_in_the_standard_envelope(client, setup):
    body = client.get(setup.url("my-readings"), **setup.mine).json()

    assert {"count", "page", "page_size", "total_pages", "next", "previous", "results", "statistics"} <= set(body)
    assert body["count"] == 3
    assert [r["systolic_bp"] for r in body["results"]] == ["120.00", "130.00", "112.00"] or [
        float(r["systolic_bp"]) for r in body["results"]
    ] == [120.0, 130.0, 112.0]
    assert body["statistics"] == {}


def test_a_reading_row_hides_the_device_but_shows_the_values_and_time(client, setup):
    row = client.get(setup.url("my-readings"), **setup.mine).json()["results"][0]

    assert "device" not in row
    assert row["recorded_at"] and row["source"] and row["id"]
    assert float(row["systolic_bp"]) == 120.0


def test_the_period_filters_work(client, setup):
    base = setup.url("my-readings")

    assert client.get(f"{base}?period=2_days", **setup.mine).json()["count"] == 1
    assert client.get(f"{base}?period=1_week", **setup.mine).json()["count"] == 2
    assert client.get(f"{base}?period=1_month", **setup.mine).json()["count"] == 2
    assert client.get(f"{base}?period=3_months", **setup.mine).json()["count"] == 3
    assert client.get(f"{base}?period=1_year", **setup.mine).json()["count"] == 3


def test_a_custom_date_range_works(client, setup):
    start = (timezone.now() - timedelta(days=5)).date().isoformat()
    end = timezone.now().date().isoformat()

    body = client.get(f"{setup.url('my-readings')}?start_date={start}&end_date={end}", **setup.mine).json()

    assert body["count"] == 2


def test_a_bad_period_is_a_400(client, setup):
    assert client.get(f"{setup.url('my-readings')}?period=3_days", **setup.mine).status_code == 400


def test_a_garbage_since_is_a_400_not_a_crash(client, setup):
    assert client.get(f"{setup.url('my-readings')}?since=not-a-date", **setup.mine).status_code == 400


def test_reading_type_adds_the_averages_and_categories(client, setup):
    body = client.get(f"{setup.url('my-readings')}?period=1_month&reading_type=blood_pressure", **setup.mine).json()

    stats = body["statistics"]
    assert stats["average"]["systolic_bp"] == 125.0
    assert stats["min"]["systolic_bp"] == 120.0
    assert stats["max"]["systolic_bp"] == 130.0
    assert "bp_category" in stats["categories"]


def test_an_unknown_reading_type_is_a_400(client, setup):
    assert client.get(f"{setup.url('my-readings')}?reading_type=mood", **setup.mine).status_code == 400


# -- latest reading and the 30-day summary --------------------------------------------------


def test_she_sees_her_latest_reading(client, setup):
    body = client.get(setup.url("my-readings/latest"), **setup.mine).json()

    assert body["total_count"] == 3
    assert float(body["reading"]["systolic_bp"]) == 120.0
    assert "device" not in body["reading"]


def test_with_no_readings_the_latest_is_none_not_a_made_up_value(client, make_hospital, auth):
    hospital = make_hospital("No Readings Hospital")
    patient = make_patient(hospital)
    user = link_account(patient, email="empty@vitals.test")

    body = client.get(
        f"/api/pregnancies/{patient.current_pregnancy.id}/my-readings/latest/", **auth(user.email, "MotherPass!2026")
    ).json()

    assert body == {"reading": None, "total_count": 0}


def test_she_sees_the_30_day_averages_and_the_months_risk_split(client, setup):
    body = client.get(setup.url("my-vitals-summary"), **setup.mine).json()

    assert body["last_30_days_average"]["systolic_bp"] == 125.0  # the 40-day-old reading is outside the window
    assert body["risk_this_month"] is not None
    assert set(body["risk_this_month"]["percentages"]) == {"low", "medium", "high"}


# -- her risk, without the staff workflow ---------------------------------------------------------


def test_she_sees_her_current_risk_and_its_category_labels(client, setup):
    body = client.get(setup.url("my-risk"), **setup.mine).json()

    current = body["current"]
    assert current["final_risk_level"] in ("low", "medium", "high")
    assert current["final_risk_level_display"]
    for key in (
        "bp_category",
        "heart_rate_category",
        "temperature_category",
        "glucose_category",
        "hemoglobin_category",
    ):
        assert key in current
    assert current["assessed_at"]
    assert float(current["reading"]["systolic_bp"]) == 120.0
    assert "device" not in current["reading"]


def test_none_of_the_staff_review_workflow_reaches_her(client, setup):
    body = client.get(setup.url("my-risk"), **setup.mine).json()

    for assessment in [body["current"], *body["history"]]:
        for key in STAFF_RISK_KEYS:
            assert key not in assessment, key


def test_her_risk_history_defaults_to_the_last_30_days(client, setup):
    oldest = setup.pregnancy.risk_assessments.order_by("assessed_at").first()
    RiskAssessment.objects.filter(pk=oldest.pk).update(assessed_at=timezone.now() - timedelta(days=40))

    body = client.get(setup.url("my-risk"), **setup.mine).json()

    assert len(body["history"]) == 2
    assert oldest.id.hex not in {a["id"].replace("-", "") for a in body["history"]}


def test_her_risk_history_takes_the_same_periods(client, setup):
    for assessment, days in zip(setup.pregnancy.risk_assessments.order_by("assessed_at"), (40, 3, 0), strict=True):
        RiskAssessment.objects.filter(pk=assessment.pk).update(assessed_at=timezone.now() - timedelta(days=days))

    assert len(client.get(f"{setup.url('my-risk')}?period=2_days", **setup.mine).json()["history"]) == 1
    assert len(client.get(f"{setup.url('my-risk')}?period=3_months", **setup.mine).json()["history"]) == 3
    assert client.get(f"{setup.url('my-risk')}?period=nonsense", **setup.mine).status_code == 400


def test_she_cannot_use_the_staff_filters_to_probe_the_review_workflow(client, setup):
    RiskAssessment.objects.filter(pregnancy=setup.pregnancy).update(flagged_for_review=True, review_status="pending")
    plain = client.get(setup.url("my-risk"), **setup.mine).json()["history"]

    for query in ("flagged_for_review=false", "flagged_for_review=true", "review_status=reviewed", "actionable=true"):
        filtered = client.get(f"{setup.url('my-risk')}?{query}", **setup.mine).json()["history"]
        assert [a["id"] for a in filtered] == [a["id"] for a in plain], query


def test_a_high_risk_carries_the_contact_your_care_team_message(client, make_hospital, auth):
    hospital = make_hospital("High Risk Hospital")
    patient = make_patient(hospital)
    user = link_account(patient, email="high@vitals.test")
    add_reading(patient.current_pregnancy, HIGH)

    body = client.get(
        f"/api/pregnancies/{patient.current_pregnancy.id}/my-risk/", **auth(user.email, "MotherPass!2026")
    ).json()

    assert body["current"]["final_risk_level"] == "high"
    assert body["contact_care_team"] is True
    assert body["contact_message"]
    assert body["notice"], "she is told this is an automatic check, not a diagnosis"


def test_a_low_risk_has_no_contact_message(client, make_hospital, auth):
    hospital = make_hospital("Low Risk Hospital")
    patient = make_patient(hospital)
    user = link_account(patient, email="low@vitals.test")
    add_reading(patient.current_pregnancy, LOW)

    body = client.get(
        f"/api/pregnancies/{patient.current_pregnancy.id}/my-risk/", **auth(user.email, "MotherPass!2026")
    ).json()

    assert body["current"]["final_risk_level"] == "low"
    assert body["contact_care_team"] is False
    assert body["contact_message"] is None
    assert body["notice"]


def test_with_no_assessment_yet_the_risk_is_empty_not_an_error(client, make_hospital, auth):
    hospital = make_hospital("No Risk Yet Hospital")
    patient = make_patient(hospital)
    user = link_account(patient, email="norisk@vitals.test")

    response = client.get(
        f"/api/pregnancies/{patient.current_pregnancy.id}/my-risk/", **auth(user.email, "MotherPass!2026")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current"] is None and body["history"] == []
    assert body["contact_care_team"] is False


# -- whose they are, and that she cannot write ------------------------------------------------------


@pytest.mark.parametrize("name", ["my-readings", "my-readings/latest", "my-vitals-summary", "my-risk"])
def test_she_cannot_read_another_womans_data_in_the_same_hospital(client, setup, name):
    stranger = make_patient(setup.hospital, first_name="Stranger")
    add_reading(stranger.current_pregnancy, MEDIUM)

    response = client.get(setup.url(name, stranger.current_pregnancy), **setup.mine)

    assert response.status_code == 404


@pytest.mark.parametrize("name", ["my-readings", "my-readings/latest", "my-vitals-summary", "my-risk"])
def test_a_patient_of_another_hospital_gets_404(client, setup, make_hospital, auth, name):
    rival = make_hospital("Rival Vitals Hospital")
    outsider = link_account(make_patient(rival, first_name="Outsider"), email="outsider@vitals.test")

    response = client.get(setup.url(name), **auth(outsider.email, "MotherPass!2026"))

    assert response.status_code == 404


@pytest.mark.parametrize("name", ["my-readings", "my-readings/latest", "my-vitals-summary", "my-risk"])
def test_staff_cannot_use_her_endpoints(client, setup, name):
    assert client.get(setup.url(name), **setup.staff).status_code == 403


@pytest.mark.parametrize("name", ["my-readings", "my-readings/latest", "my-vitals-summary", "my-risk"])
def test_her_endpoints_need_a_signed_in_user(client, setup, name):
    assert client.get(setup.url(name)).status_code == 401


@pytest.mark.parametrize("name", ["my-readings", "my-readings/latest", "my-vitals-summary", "my-risk"])
def test_her_endpoints_are_read_only(client, setup, name):
    for method in ("post", "patch", "put", "delete"):
        response = getattr(client, method)(
            setup.url(name),
            data='{"systolic_bp": 120, "source": "manual"}',
            content_type="application/json",
            **setup.mine,
        )
        assert response.status_code == 405, f"{method} {name} -> {response.status_code}"


def test_she_still_cannot_record_a_reading_on_the_staff_endpoint(client, setup):
    before = VitalReading.objects.count()

    response = client.post(
        setup.url("readings"),
        data='{"systolic_bp": 120, "diastolic_bp": 80, "source": "manual"}',
        content_type="application/json",
        **setup.mine,
    )

    assert response.status_code == 403
    assert VitalReading.objects.count() == before


def test_the_staff_reading_endpoints_are_unchanged_for_staff(client, setup):
    body = client.get(setup.url("readings"), **setup.staff).json()

    assert body["count"] == 3
    assert "device" in body["results"][0], "staff still get the full row"
