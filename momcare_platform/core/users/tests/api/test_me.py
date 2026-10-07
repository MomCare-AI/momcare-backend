"""/api/auth/me/ - the signed-in user's own profile.

``staff_id`` exists so the frontend can match "me" against a care-team
row's staff id without a second round-trip (e.g. to decide whether to show
a pregnancy's care-team write controls to the logged-in user).
"""

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db

ME = "/api/auth/me/"


def test_a_staff_linked_user_gets_their_own_staff_id(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Me Staff Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@mestaff.test")

    response = client.get(ME, **auth(nurse.email))

    assert response.status_code == 200
    assert response.json()["staff_id"] == str(nurse.staff.id)


def test_a_user_with_no_staff_row_gets_a_null_staff_id(client, make_hospital, auth):
    """hospital_admin, in this fixture, has no Staff row - the field must
    say so honestly rather than erroring or omitting itself."""
    hospital = make_hospital("Me No Staff Hospital")

    response = client.get(ME, **auth(hospital.admin.email))

    assert response.status_code == 200
    assert response.json()["staff_id"] is None


# -- a patient's own ids ----------------------------------------------------------
# Every URL a patient may call is built from her pregnancy id, and she has no patient list
# to find it in. Only a patient account gets these two keys.

STAFF_ME_KEYS = {
    "id",
    "email",
    "first_name",
    "last_name",
    "phone",
    "gender",
    "role_code",
    "organization_id",
    "organization_name",
    "staff_id",
    "is_email_verified",
    "requires_password_reset",
    "created_at",
}


def _patient_account(hospital, make_patient, patient_user, email):
    patient = make_patient(hospital)
    return patient, patient_user(patient, email=email)


@pytest.fixture
def make_patient(make_hospital):
    from datetime import timedelta  # noqa: PLC0415

    from django.utils import timezone  # noqa: PLC0415

    from momcare_platform.core.patients.services import onboard_patient  # noqa: PLC0415

    def _make(hospital, first_name="Ayesha"):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        )

    return _make


@pytest.fixture
def patient_user(db):
    from momcare_platform.core.users.models import Role, User  # noqa: PLC0415

    def _make(patient, email="mother@me.test", password="MotherPass!2026"):
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
        user.raw_password = password
        return user

    return _make


def test_a_patient_gets_her_own_patient_and_pregnancy_ids(client, make_hospital, make_patient, patient_user, auth):
    hospital = make_hospital("Me Patient Hospital")
    patient, user = _patient_account(hospital, make_patient, patient_user, "mother@mepatient.test")

    body = client.get(ME, **auth(user.email, user.raw_password)).json()

    assert body["role_code"] == settings.ROLE_PATIENT
    assert body["patient_id"] == str(patient.id)
    assert body["pregnancy_id"] == str(patient.current_pregnancy.id)


def test_each_patient_gets_only_her_own_ids(client, make_hospital, make_patient, patient_user, auth):
    hospital = make_hospital("Me Two Patients Hospital")
    first, first_user = _patient_account(hospital, make_patient, patient_user, "one@metwo.test")
    second = make_patient(hospital, "Sana")
    second_user = patient_user(second, email="two@metwo.test")

    one = client.get(ME, **auth(first_user.email, first_user.raw_password)).json()
    two = client.get(ME, **auth(second_user.email, second_user.raw_password)).json()

    assert one["pregnancy_id"] == str(first.current_pregnancy.id)
    assert two["pregnancy_id"] == str(second.current_pregnancy.id)
    assert one["pregnancy_id"] != two["pregnancy_id"]


def test_the_pregnancy_id_a_patient_gets_opens_her_care_plan_url(
    client, make_hospital, make_patient, patient_user, auth
):
    hospital = make_hospital("Me Plan Url Hospital")
    patient, user = _patient_account(hospital, make_patient, patient_user, "mother@meurl.test")
    headers = auth(user.email, user.raw_password)

    pregnancy_id = client.get(ME, **headers).json()["pregnancy_id"]
    response = client.get(f"/api/pregnancies/{pregnancy_id}/current-care-plan/", **headers)

    assert response.status_code == 200  # her own plan URL is reachable with the id she was given


def test_a_patient_with_no_active_pregnancy_gets_a_null_pregnancy_id(
    client, make_hospital, make_patient, patient_user, auth
):
    from momcare_platform.core.patients.models import Pregnancy  # noqa: PLC0415

    hospital = make_hospital("Me No Pregnancy Hospital")
    patient, user = _patient_account(hospital, make_patient, patient_user, "mother@menopreg.test")
    Pregnancy.objects.filter(patient=patient).update(status=Pregnancy.STATUS_DELIVERED)

    body = client.get(ME, **auth(user.email, user.raw_password)).json()

    assert body["patient_id"] == str(patient.id)
    assert body["pregnancy_id"] is None


def test_staff_and_admin_responses_are_exactly_what_they_were(client, make_hospital, make_staff, auth):
    """The doctor side must not change by even a key."""
    hospital = make_hospital("Me Unchanged Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@meunchanged.test")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "doc@meunchanged.test")

    for email in (nurse.email, provider.email, hospital.admin.email):
        body = client.get(ME, **auth(email)).json()
        assert set(body) == STAFF_ME_KEYS, email
        assert "patient_id" not in body and "pregnancy_id" not in body


def test_the_login_response_is_not_changed_for_anyone(client, make_hospital, make_staff):
    hospital = make_hospital("Me Login Hospital")
    make_staff(hospital.org, settings.ROLE_NURSE, "nurse@melogin.test")

    body = client.post("/api/auth/login/", data={"email": "nurse@melogin.test", "password": "TestPass!2026"}).json()

    assert "patient_id" not in body["user"] and "pregnancy_id" not in body["user"]
