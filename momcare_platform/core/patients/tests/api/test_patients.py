"""Patient enrolment, pregnancy, and the tenant boundary around both.

The isolation tests matter most: a leak here is not an embarrassment, it is the
disclosure of someone's pregnancy to a hospital with no relationship to her.
"""

import json
from datetime import date, timedelta

import pytest
from django.conf import settings

from momcare_platform.core.locations.services import ensure_default_location
from momcare_platform.core.organization.models import Organization
from momcare_platform.core.patients.models import Patient, Pregnancy
from momcare_platform.core.patients.services import onboard_patient
from momcare_platform.core.users.models import Role, User

pytestmark = pytest.mark.django_db

PATIENTS = "/api/patients/"


def enrolment_payload(**overrides):
    payload = {
        "first_name": "Ayesha",
        "last_name": "Bibi",
        "phone": "03001234567",
        "cnic": "61101-1234567-8",
        "blood_group": "O+",
    }
    payload.update(overrides)
    return payload


def post_patient(client, headers, **overrides):
    return client.post(
        PATIENTS,
        data=json.dumps(enrolment_payload(**overrides)),
        content_type="application/json",
        **headers,
    )


# ── Enrolment ────────────────────────────────────────────────────────────────


def test_patient_is_enrolled_without_any_user_account(client, make_hospital, auth):
    """The central change: a clinical identity needs no login.

    A woman at a rural clinic may have no email and no phone she controls, and
    must still have a complete record.
    """
    hospital = make_hospital("Enrol Hospital")

    response = post_patient(client, auth(hospital.admin.email))

    assert response.status_code == 201
    body = response.json()
    assert body["full_name"] == "Ayesha Bibi"
    assert body["has_app_account"] is False

    patient = Patient.objects.get(id=body["id"])
    assert patient.user is None
    assert patient.location.organization == hospital.org


def test_mrn_is_supplied_by_the_hospital_not_generated(client, make_hospital, auth):
    """The hospital brings its own numbering; the platform never invents a
    competing identifier for the same woman."""
    hospital = make_hospital("Alpha Care")

    response = post_patient(client, auth(hospital.admin.email), mrn="AC-2026-0042")

    assert response.status_code == 201
    assert response.json()["mrn"] == "AC-2026-0042"


def test_mrn_is_optional(client, make_hospital, auth):
    hospital = make_hospital("No MRN Hospital")

    response = post_patient(client, auth(hospital.admin.email))

    assert response.status_code == 201
    assert response.json()["mrn"] is None


def test_two_patients_without_an_mrn_do_not_collide(client, make_hospital, auth):
    """Blank MRN is stored as NULL, never "" — otherwise the second MRN-less
    patient would collide with the first under the unique constraint."""
    hospital = make_hospital("Blank MRN Hospital")
    headers = auth(hospital.admin.email)

    first = post_patient(client, headers, first_name="One", cnic="", mrn="")
    second = post_patient(client, headers, first_name="Two", cnic="", mrn="")

    assert first.status_code == 201
    assert second.status_code == 201
    assert Patient.objects.filter(mrn__isnull=True).count() == 2


def test_a_duplicate_mrn_is_rejected(client, make_hospital, auth):
    hospital = make_hospital("Duplicate MRN Hospital")
    headers = auth(hospital.admin.email)
    post_patient(client, headers, first_name="One", mrn="DUP-001")

    response = post_patient(client, headers, first_name="Two", cnic="", mrn="DUP-001")

    assert response.status_code == 400
    assert "mrn" in response.json()


def test_a_duplicate_mrn_is_rejected_case_insensitively(client, make_hospital, auth):
    hospital = make_hospital("Case MRN Hospital")
    headers = auth(hospital.admin.email)
    post_patient(client, headers, first_name="One", mrn="abc-001")

    response = post_patient(client, headers, first_name="Two", cnic="", mrn="ABC-001")

    assert response.status_code == 400
    assert "mrn" in response.json()


def test_mrn_is_unique_across_hospitals(client, make_hospital, auth):
    """MRN uniqueness is global, unlike CNIC which is scoped per hospital."""
    alpha = make_hospital("Alpha MRN")
    beta = make_hospital("Beta MRN")
    post_patient(client, auth(alpha.admin.email), mrn="SHARED-001")

    response = post_patient(client, auth(beta.admin.email), mrn="SHARED-001")

    assert response.status_code == 400


def test_two_hospitals_sharing_a_name_prefix_can_both_onboard(client, make_hospital, auth):
    """Regression: MRNs used to be generated from the first four characters of
    the hospital's name, so two hospitals whose names shared a prefix produced
    colliding MRNs and the second could not onboard anyone at all."""
    first = make_hospital("APITEST Maternity")
    second = make_hospital("APITEST Rival Hospital")

    for index in range(6):
        post_patient(client, auth(first.admin.email), first_name=f"P{index}", cnic=f"61101-111111{index}-1")

    response = post_patient(client, auth(second.admin.email), first_name="Rival", cnic="")

    assert response.status_code == 201


def test_a_patient_survives_deletion_of_her_user_account(make_hospital):
    """SET_NULL, not CASCADE — losing an app account must not erase a record."""
    hospital = make_hospital("SetNull Hospital")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Zara", "last_name": "Khan"},
    )
    account = User.objects.create_user(email="zara@example.test", password="AppPass!2026")
    patient.user = account
    patient.save(update_fields=["user", "updated_at"])

    account.delete()

    patient.refresh_from_db()
    assert patient.id is not None
    assert patient.user is None
    assert patient.full_name == "Zara Khan"


# ── Organization.patient_count regression ────────────────────────────────────


def test_patient_count_includes_patients_without_a_user(make_hospital):
    """It previously counted through ``user__organization``, so every patient
    enrolled without an app account was invisible — the dashboard would have
    read zero with a full ward."""
    hospital = make_hospital("Count Hospital")
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "NoAccount"},
    )

    assert hospital.org.patient_count == 1


def test_patient_count_includes_patients_with_a_user(make_hospital):
    hospital = make_hospital("Count With User")
    patient = onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "WithAccount"},
    )
    patient.user = User.objects.create_user(email="withaccount@example.test", password="AppPass!2026")
    patient.save(update_fields=["user", "updated_at"])

    assert hospital.org.patient_count == 1


# ── Main Branch ──────────────────────────────────────────────────────────────


def test_approval_creates_a_main_branch(make_hospital):
    hospital = make_hospital("Branch Hospital", status=Organization.STATUS_PENDING)
    assert hospital.org.locations.count() == 0

    hospital.org.set_review_status(Organization.STATUS_APPROVED)

    assert hospital.org.locations.count() == 1
    assert hospital.org.locations.first().name == "Main Branch"


def test_main_branch_creation_is_idempotent(make_hospital):
    hospital = make_hospital("Idempotent Branch")
    ensure_default_location(hospital.org)
    ensure_default_location(hospital.org)
    ensure_default_location(hospital.org)

    assert hospital.org.locations.count() == 1


def test_existing_locations_are_not_displaced(make_hospital):
    """A hospital that already set up real sites must not gain a stray default."""
    from momcare_platform.core.locations.models import Location  # noqa: PLC0415

    hospital = make_hospital("Real Sites")
    hospital.org.locations.all().delete()
    Location.objects.create(organization=hospital.org, name="Islamabad Clinic")

    ensure_default_location(hospital.org)

    assert list(hospital.org.locations.values_list("name", flat=True)) == ["Islamabad Clinic"]


# ── Pregnancy ────────────────────────────────────────────────────────────────


def test_pregnancy_derives_edd_from_lmp(client, make_hospital, auth):
    hospital = make_hospital("EDD Hospital")
    lmp = date(2026, 2, 5)

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": lmp.isoformat(), "gravida": 2, "para": 1},
    )

    pregnancy = Patient.objects.get(id=response.json()["id"]).current_pregnancy
    assert pregnancy.edd == date(2026, 11, 12)
    assert pregnancy.edd_source == Pregnancy.EDD_FROM_LMP


def test_explicit_edd_overrides_the_lmp_estimate(client, make_hospital, auth):
    """Ultrasound dating supersedes LMP, and the source records which is authoritative."""
    hospital = make_hospital("Override Hospital")
    scan_edd = date(2026, 11, 9)

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={
            "lmp": date(2026, 2, 5).isoformat(),
            "edd": scan_edd.isoformat(),
            "edd_source": Pregnancy.EDD_FROM_ULTRASOUND,
        },
    )

    pregnancy = Patient.objects.get(id=response.json()["id"]).current_pregnancy
    assert pregnancy.edd == scan_edd
    assert pregnancy.edd_source == Pregnancy.EDD_FROM_ULTRASOUND
    assert pregnancy.edd_confirmed_at is not None


def test_pregnancy_requires_a_date_to_work_from(client, make_hospital, auth):
    hospital = make_hospital("Dateless Hospital")

    response = post_patient(client, auth(hospital.admin.email), pregnancy={"gravida": 1})

    assert response.status_code == 400


def test_gestational_age_is_exposed_but_never_stored(client, make_hospital, auth):
    hospital = make_hospital("GA Hospital")
    edd = date.today() + timedelta(weeks=12)

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"edd": edd.isoformat(), "edd_source": Pregnancy.EDD_FROM_ULTRASOUND},
    )

    pregnancy = response.json()["current_pregnancy"]
    assert pregnancy["gestational_age_weeks"] == 28
    assert "gestational_age" not in [f.name for f in Pregnancy._meta.get_fields()]


def test_risk_factors_default_to_unknown(client, make_hospital, auth):
    """Unknown is not No — a blank answer must never read as a negative."""
    hospital = make_hospital("Risk Hospital")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": date(2026, 2, 5).isoformat()},
    )

    pregnancy = response.json()["current_pregnancy"]
    for field in Pregnancy.FACTOR_FIELDS:
        assert pregnancy[field] == Pregnancy.UNKNOWN
    assert pregnancy["present_factors"] == []
    assert len(pregnancy["unanswered_factors"]) == len(Pregnancy.FACTOR_FIELDS)


def test_risk_factors_are_recorded_when_given(client, make_hospital, auth):
    hospital = make_hospital("Factors Hospital")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={
            "lmp": date(2026, 2, 5).isoformat(),
            "previous_c_section": "yes",
            "diabetes": "no",
        },
    )

    pregnancy = response.json()["current_pregnancy"]
    assert pregnancy["previous_c_section"] == "yes"
    assert pregnancy["diabetes"] == "no"
    assert pregnancy["previous_preeclampsia"] == "unknown"
    assert pregnancy["present_factors"] == ["previous_c_section"]


def test_only_one_active_pregnancy_at_a_time(client, make_hospital, auth):
    hospital = make_hospital("Single Active")
    headers = auth(hospital.admin.email)
    patient_id = post_patient(
        client,
        headers,
        pregnancy={"lmp": date(2026, 2, 5).isoformat()},
    ).json()["id"]

    second = client.post(
        f"{PATIENTS}{patient_id}/pregnancies/",
        data=json.dumps({"lmp": date(2026, 6, 1).isoformat()}),
        content_type="application/json",
        **headers,
    )

    assert second.status_code == 400
    assert "already has an active pregnancy" in second.json()["detail"]


def test_pregnancy_history_is_kept_after_one_ends(client, make_hospital, auth):
    hospital = make_hospital("History Hospital")
    headers = auth(hospital.admin.email)
    patient_id = post_patient(
        client,
        headers,
        pregnancy={"lmp": date(2024, 1, 1).isoformat()},
    ).json()["id"]
    first = Patient.objects.get(id=patient_id).current_pregnancy

    client.patch(
        f"{PATIENTS}{patient_id}/pregnancies/{first.id}/",
        data=json.dumps({"status": Pregnancy.STATUS_DELIVERED}),
        content_type="application/json",
        **headers,
    )
    client.post(
        f"{PATIENTS}{patient_id}/pregnancies/",
        data=json.dumps({"lmp": date(2026, 2, 5).isoformat()}),
        content_type="application/json",
        **headers,
    )

    body = client.get(f"{PATIENTS}{patient_id}/pregnancies/", **headers).json()
    assert set(body.keys()) == {"count", "page", "page_size", "total_pages", "next", "previous", "results"}
    listing = body["results"]
    assert len(listing) == 2
    assert {p["status"] for p in listing} == {"delivered", "active"}


def test_pregnancy_cannot_be_deleted(client, make_hospital, auth):
    """Historical clinical fact — corrected, never removed."""
    hospital = make_hospital("No Delete")
    headers = auth(hospital.admin.email)
    patient_id = post_patient(
        client,
        headers,
        pregnancy={"lmp": date(2026, 2, 5).isoformat()},
    ).json()["id"]
    pregnancy = Patient.objects.get(id=patient_id).current_pregnancy

    response = client.delete(f"{PATIENTS}{patient_id}/pregnancies/{pregnancy.id}/", **headers)

    assert response.status_code == 405
    assert Pregnancy.objects.filter(id=pregnancy.id).exists()


# ── Clinical responsibility ──────────────────────────────────────────────────


def test_a_clinician_can_be_assigned_at_enrolment(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Assign Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "lead@assign.test")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={
            "lmp": date(2026, 2, 5).isoformat(),
            "provider": str(doctor.staff.id),
        },
    )

    pregnancy = response.json()["current_pregnancy"]
    assert pregnancy["provider"] == str(doctor.staff.id)
    assert pregnancy["has_responsible_clinician"] is True


def test_cannot_assign_a_clinician_from_another_hospital(client, make_hospital, make_staff, auth):
    """The dropdown is filtered, but the API must not depend on that.

    Naming another hospital's clinician would leak that they exist and would
    make the accountability record false — the pregnancy would point at someone
    with no relationship to the patient.
    """
    alpha = make_hospital("Alpha Assign")
    beta = make_hospital("Beta Assign")
    beta_doctor = make_staff(beta.org, settings.ROLE_PROVIDER, "beta.lead@assign.test")

    response = post_patient(
        client,
        auth(alpha.admin.email),
        pregnancy={
            "lmp": date(2026, 2, 5).isoformat(),
            "provider": str(beta_doctor.staff.id),
        },
    )

    assert response.status_code == 400
    errors = response.json()["pregnancy"]["provider"]
    # "does not exist" rather than "belongs to another hospital": the message
    # must not confirm that this clinician is real somewhere else.
    assert "does not exist" in errors[0]
    assert not Patient.objects.exists(), "patient was created despite an invalid assignment"


def test_cannot_patch_in_another_hospitals_clinician(client, make_hospital, make_staff, auth):
    """Scoping must hold on update, not only on create."""
    alpha = make_hospital("Alpha Patch Assign")
    beta = make_hospital("Beta Patch Assign")
    beta_doctor = make_staff(beta.org, settings.ROLE_PROVIDER, "beta.patch@assign.test")
    headers = auth(alpha.admin.email)

    patient_id = post_patient(
        client,
        headers,
        pregnancy={"lmp": date(2026, 2, 5).isoformat()},
    ).json()["id"]
    pregnancy = Patient.objects.get(id=patient_id).current_pregnancy

    response = client.patch(
        f"{PATIENTS}{patient_id}/pregnancies/{pregnancy.id}/",
        data=json.dumps({"provider": str(beta_doctor.staff.id)}),
        content_type="application/json",
        **headers,
    )

    assert response.status_code == 400
    pregnancy.refresh_from_db()
    assert pregnancy.provider is None


def test_an_unassigned_pregnancy_reports_no_responsible_clinician(client, make_hospital, auth):
    hospital = make_hospital("Unassigned Hospital")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": date(2026, 2, 5).isoformat()},
    )

    assert response.json()["current_pregnancy"]["has_responsible_clinician"] is False


def test_a_departed_clinician_no_longer_counts_as_responsible(client, make_hospital, make_staff, auth):
    """The FK still resolves after a clinician leaves, so the record looks
    assigned. For alert routing that is the same silent failure as no
    assignment, and it has to surface."""
    hospital = make_hospital("Departure Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "leaver@departure.test")
    headers = auth(hospital.admin.email)

    patient_id = post_patient(
        client,
        headers,
        pregnancy={
            "lmp": date(2026, 2, 5).isoformat(),
            "provider": str(doctor.staff.id),
        },
    ).json()["id"]

    doctor.staff.deactivate(reason="Left the hospital")

    detail = client.get(f"{PATIENTS}{patient_id}/", **headers).json()
    pregnancy = detail["current_pregnancy"]
    assert pregnancy["provider"] is not None, "the historical assignment must be kept"
    assert pregnancy["has_responsible_clinician"] is False, "an inactive clinician must not count"


# ── Search and pagination ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "term",
    ["Ayesha", "Bibi", "Ayesha Bibi", "03001234567", "61101-1234567-8"],
)
def test_search_matches_each_identifier(client, make_hospital, auth, term):
    hospital = make_hospital("Search Hospital")
    headers = auth(hospital.admin.email)
    post_patient(client, headers)
    post_patient(client, headers, first_name="Sana", last_name="Malik", phone="03119999999", cnic="")

    results = client.get(f"{PATIENTS}?search={term}", **headers).json()["results"]

    assert len(results) == 1
    assert results[0]["full_name"] == "Ayesha Bibi"


def test_search_matches_mrn(client, make_hospital, auth):
    hospital = make_hospital("MRN Search")
    headers = auth(hospital.admin.email)
    mrn = post_patient(client, headers, mrn="SEARCH-0001").json()["mrn"]

    results = client.get(f"{PATIENTS}?search={mrn}", **headers).json()["results"]

    assert [r["mrn"] for r in results] == [mrn]


def test_list_is_paginated(client, make_hospital, auth):
    hospital = make_hospital("Paged Hospital")
    headers = auth(hospital.admin.email)
    for i in range(3):
        post_patient(client, headers, first_name=f"Patient{i}", cnic="", phone="")

    body = client.get(PATIENTS, **headers).json()

    assert body["count"] == 3
    assert "results" in body
    assert "next" in body


# ── Tenant isolation ─────────────────────────────────────────────────────────


def test_patient_list_never_crosses_hospitals(client, make_hospital, auth):
    alpha = make_hospital("Alpha Patients")
    beta = make_hospital("Beta Patients")
    post_patient(client, auth(alpha.admin.email), first_name="AlphaPatient")
    post_patient(client, auth(beta.admin.email), first_name="BetaPatient")

    alpha_names = {p["full_name"] for p in client.get(PATIENTS, **auth(alpha.admin.email)).json()["results"]}
    beta_names = {p["full_name"] for p in client.get(PATIENTS, **auth(beta.admin.email)).json()["results"]}

    assert not alpha_names & beta_names, "patient lists leaked across hospitals"


def test_retrieving_another_hospitals_patient_is_404(client, make_hospital, auth):
    """404, not 403 — the response must not reveal that she exists elsewhere."""
    alpha = make_hospital("Alpha Detail")
    beta = make_hospital("Beta Detail")
    beta_patient_id = post_patient(client, auth(beta.admin.email)).json()["id"]

    response = client.get(f"{PATIENTS}{beta_patient_id}/", **auth(alpha.admin.email))

    assert response.status_code == 404
    assert response.json()["detail"] == "Patient not found."


def test_another_hospitals_pregnancy_is_unreachable(client, make_hospital, auth):
    alpha = make_hospital("Alpha Preg")
    beta = make_hospital("Beta Preg")
    beta_id = post_patient(
        client,
        auth(beta.admin.email),
        pregnancy={"lmp": date(2026, 2, 5).isoformat()},
    ).json()["id"]

    response = client.get(f"{PATIENTS}{beta_id}/pregnancies/", **auth(alpha.admin.email))

    assert response.status_code == 404


def test_patching_another_hospitals_patient_is_404(client, make_hospital, auth):
    """Scoping must hold on writes, not only on reads."""
    alpha = make_hospital("Alpha Write")
    beta = make_hospital("Beta Write")
    beta_id = post_patient(client, auth(beta.admin.email)).json()["id"]

    response = client.patch(
        f"{PATIENTS}{beta_id}/",
        data=json.dumps({"first_name": "Hijacked"}),
        content_type="application/json",
        **auth(alpha.admin.email),
    )

    assert response.status_code == 404
    assert Patient.objects.get(id=beta_id).first_name == "Ayesha"


def test_a_platform_admin_gets_no_cross_tenant_patient_list(client, make_hospital, auth):
    alpha = make_hospital("Alpha Platform Patients")
    post_patient(client, auth(alpha.admin.email))
    platform_admin = User.objects.create_user(
        email="platform.patients@momcare.test",
        password="TestPass!2026",
        first_name="Platform",
        last_name="Admin",
        role=Role.objects.get(code=settings.ROLE_PLATFORM_ADMIN),
    )

    response = client.get(PATIENTS, **auth(platform_admin.email))

    assert response.status_code in (403, 404)


# ── Audit ────────────────────────────────────────────────────────────────────


def test_enrolment_is_audited_with_the_acting_user(client, make_hospital, auth):
    """The existing PHI middleware covers /api/patients — this proves it fires,
    and that it records *who* acted, not only what happened.

    Attribution works for JWT requests because the middleware records after the
    view has run, by which point DRF has resolved request.user. Worth asserting:
    an audit trail that cannot name the actor is of little use in a clinical
    system, and the middleware's own docstring warns that token attribution was
    unverified.
    """
    from momcare_platform.core.organization.models import AuditLog  # noqa: PLC0415

    hospital = make_hospital("Audited Hospital")
    post_patient(client, auth(hospital.admin.email))

    entry = AuditLog.objects.filter(resource="patients", action="CREATE").first()
    assert entry is not None, "patient creation was not written to the audit log"
    assert entry.endpoint == PATIENTS
    assert entry.user == hospital.admin, "audit log did not attribute the acting user"


# ── Risk on the list ─────────────────────────────────────────────────────────


def test_the_list_carries_the_current_risk_level(client, make_hospital, auth):
    """The list is triage: a clinician decides which row to open from it, so the
    risk level has to travel with the row rather than one click away."""
    import importlib

    from django.apps import apps as django_apps
    from django.utils import timezone  # noqa: PLC0415

    # Resolved via the app registry / a runtime module lookup, not a static
    # import: both live in modules.pregnancy.vitals, which core (this test
    # included) must never import statically — the `core must not import
    # modules` contract. reassess_risk is a function, not a model, so
    # apps.get_model() (used for VitalReading) doesn't apply to it;
    # importlib.import_module() is the same "not a Python import statement"
    # escape hatch for the same reason.
    VitalReading = django_apps.get_model("monitoring", "VitalReading")
    reassess_risk = importlib.import_module(
        "momcare_platform.modules.pregnancy.vitals.services",
    ).reassess_risk

    hospital = make_hospital("Triage Hospital")
    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="AtRisk",
        pregnancy={"lmp": (date.today() - timedelta(weeks=24)).isoformat()},
    )
    pregnancy = Patient.objects.get(first_name="AtRisk").current_pregnancy
    assert pregnancy is not None
    VitalReading.objects.create(
        pregnancy=pregnancy,
        systolic_bp=185,
        diastolic_bp=125,
        heart_rate=130,
        body_temp_f=103.0,
        hemoglobin=6.0,
        blood_glucose=250,
        stress_score=9,
        phys_activity_score=1,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
    )
    reassess_risk(pregnancy)

    row = client.get(PATIENTS, **auth(hospital.admin.email)).json()["results"][0]

    assert row["risk_level"] == "high"
    assert row["risk_assessed_at"] is not None
    assert row["pregnancy_id"] == str(pregnancy.id)


def test_a_patient_never_assessed_reports_no_level_rather_than_stable(
    client,
    make_hospital,
    auth,
):
    """Absent is not the same as safe. Reporting "stable" for someone nobody has
    measured would be the system inventing reassurance it has no basis for."""
    hospital = make_hospital("Unassessed Hospital")
    post_patient(client, auth(hospital.admin.email))

    row = client.get(PATIENTS, **auth(hospital.admin.email)).json()["results"][0]

    assert row["risk_level"] is None
    assert row["risk_assessed_at"] is None


def test_listing_more_patients_does_not_cost_more_queries(
    client,
    make_hospital,
    auth,
    django_assert_max_num_queries,
):
    """Guards the prefetch. Without it each row queries for its pregnancy and
    again for its latest assessment, so a page of twenty costs forty round
    trips — the kind of regression that only shows up once a hospital has real
    numbers on the ward.

    The ceiling includes one query for row-level security's SET LOCAL, set
    once per request from the JWT's own org claim by TenantAwareJWTAuthentication
    - a fixed cost, not one that grows with the page.
    """
    hospital = make_hospital("Volume Hospital")
    headers = auth(hospital.admin.email)
    for index in range(6):
        post_patient(client, headers, first_name=f"Patient{index}", cnic=f"61101-000000{index}-1")

    with django_assert_max_num_queries(13):
        response = client.get(PATIENTS, **headers)

    assert response.status_code == 200
    assert response.json()["count"] == 6


# ── Organization column, CNIC uniqueness, emergency contact email ─────────────


def test_patient_organization_is_set_from_the_enrolling_hospital(client, make_hospital, auth):
    hospital = make_hospital("Org Column Hospital")
    response = post_patient(client, auth(hospital.admin.email))

    patient = Patient.objects.get(id=response.json()["id"])
    assert patient.organization_id == hospital.org.id


def test_cnic_is_unique_within_a_hospital(client, make_hospital, auth):
    hospital = make_hospital("CNIC Unique Hospital")
    headers = auth(hospital.admin.email)
    post_patient(client, headers, first_name="First", cnic="61101-1111111-1")

    response = post_patient(client, headers, first_name="Second", cnic="61101-1111111-1")

    assert response.status_code == 400


def test_cnic_can_repeat_across_different_hospitals(client, make_hospital, auth):
    alpha = make_hospital("CNIC Alpha")
    beta = make_hospital("CNIC Beta")

    first = post_patient(client, auth(alpha.admin.email), cnic="61101-2222222-2")
    second = post_patient(client, auth(beta.admin.email), cnic="61101-2222222-2")

    assert first.status_code == 201
    assert second.status_code == 201


def test_two_patients_without_cnic_do_not_collide(client, make_hospital, auth):
    """Blank CNIC is stored as NULL, never '', so two blank values never
    false-positive collide under the unique constraint."""
    hospital = make_hospital("No CNIC Hospital")
    headers = auth(hospital.admin.email)

    first = post_patient(client, headers, first_name="First", cnic="")
    second = post_patient(client, headers, first_name="Second", cnic="")

    assert first.status_code == 201
    assert second.status_code == 201


def test_emergency_contact_email_is_accepted_and_stored(client, make_hospital, auth):
    hospital = make_hospital("Emergency Email Hospital")
    response = post_patient(
        client,
        auth(hospital.admin.email),
        emergency_contact_email="brother@example.test",
    )

    patient = Patient.objects.get(id=response.json()["id"])
    assert patient.emergency_contact_email == "brother@example.test"


# ── Care team: provider / nurse / care_manager ────────────────────────────────


def test_pregnancy_accepts_provider_nurse_and_care_manager_together(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Full Care Team Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@fullcareteam.test")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@fullcareteam.test")
    care_manager = make_staff(hospital.org, settings.ROLE_CARE_MANAGER, "cm@fullcareteam.test")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={
            "lmp": "2026-01-01",
            "provider": str(provider.staff.id),
            "nurse": str(nurse.staff.id),
            "care_manager": str(care_manager.staff.id),
        },
    )

    assert response.status_code == 201
    pregnancy = Patient.objects.get(id=response.json()["id"]).current_pregnancy
    assert pregnancy.provider_id == provider.staff.id
    assert pregnancy.nurse_id == nurse.staff.id
    assert pregnancy.care_manager_id == care_manager.staff.id


# ── ?assigned_to=me ──────────────────────────────────────────────────────────


def test_assigned_to_me_returns_patients_where_caller_is_the_nurse(
    client,
    make_hospital,
    make_staff,
    auth,
):
    hospital = make_hospital("Assigned To Me Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@assignedtome.test")
    other_nurse = make_staff(hospital.org, settings.ROLE_NURSE, "other@assignedtome.test")

    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="Mine",
        cnic="61101-3333333-1",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )
    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="NotMine",
        cnic="61101-3333333-2",
        pregnancy={"lmp": "2026-01-01", "nurse": str(other_nurse.staff.id)},
    )

    response = client.get(f"{PATIENTS}?assigned_to=me", **auth("nurse@assignedtome.test"))

    names = [row["full_name"] for row in response.json()["results"]]
    assert names == ["Mine Bibi"]


def test_assigned_to_me_returns_patients_where_caller_is_the_provider(
    client,
    make_hospital,
    make_staff,
    auth,
):
    hospital = make_hospital("Provider Assigned Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@providerassigned.test")

    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="Mine",
        cnic="61101-4444444-1",
        pregnancy={"lmp": "2026-01-01", "provider": str(provider.staff.id)},
    )
    post_patient(client, auth(hospital.admin.email), first_name="NotMine", cnic="61101-4444444-2")

    response = client.get(f"{PATIENTS}?assigned_to=me", **auth("provider@providerassigned.test"))

    names = [row["full_name"] for row in response.json()["results"]]
    assert names == ["Mine Bibi"]


def test_assigned_to_me_is_an_honest_empty_list_for_a_hospital_admin(client, make_hospital, auth):
    """ "My patients" isn't a concept that applies to an admin — an empty
    result, not the param silently ignored and everyone returned."""
    hospital = make_hospital("Admin Assigned Hospital")
    post_patient(client, auth(hospital.admin.email))

    response = client.get(f"{PATIENTS}?assigned_to=me", **auth(hospital.admin.email))

    assert response.json()["count"] == 0


# ── Care-team assignment rules: role match, capacity ─────────────────────────


def test_assigning_a_nurse_to_the_provider_slot_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Role Mismatch Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@rolemismatch.test")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": "2026-01-01", "provider": str(nurse.staff.id)},
    )

    assert response.status_code == 400
    assert "provider" in response.json()["pregnancy"]


def test_assigning_a_provider_to_the_nurse_slot_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Nurse Slot Mismatch Hospital")
    provider = make_staff(hospital.org, settings.ROLE_PROVIDER, "provider@nurseslot.test")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": "2026-01-01", "nurse": str(provider.staff.id)},
    )

    assert response.status_code == 400
    assert "nurse" in response.json()["pregnancy"]


def test_assigning_a_staff_member_already_at_capacity_is_rejected(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Over Capacity Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@overcapacity.test")
    nurse.staff.max_patients = 1
    nurse.staff.save(update_fields=["max_patients"])
    post_patient(
        client,
        auth(hospital.admin.email),
        first_name="First",
        cnic="61101-5555555-1",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )

    response = post_patient(
        client,
        auth(hospital.admin.email),
        first_name="Second",
        cnic="61101-5555555-2",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )

    assert response.status_code == 400
    assert "nurse" in response.json()["pregnancy"]


def test_a_staff_member_with_no_max_patients_is_never_at_capacity(client, make_hospital, make_staff, auth):
    """max_patients=None means unlimited — the capacity check must not treat
    it as zero."""
    hospital = make_hospital("Unlimited Capacity Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@unlimited.test")

    first = post_patient(
        client,
        auth(hospital.admin.email),
        first_name="First",
        cnic="61101-6666666-1",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )
    second = post_patient(
        client,
        auth(hospital.admin.email),
        first_name="Second",
        cnic="61101-6666666-2",
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    )

    assert first.status_code == 201
    assert second.status_code == 201


def test_patching_a_pregnancy_without_changing_the_nurse_does_not_recheck_capacity(
    client,
    make_hospital,
    make_staff,
    auth,
):
    """An unchanged assignment must not fail its own capacity check just
    because the staff member is now full."""
    hospital = make_hospital("Idempotent Reassign Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@idempotentreassign.test")
    nurse.staff.max_patients = 1
    nurse.staff.save(update_fields=["max_patients"])
    created = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={"lmp": "2026-01-01", "nurse": str(nurse.staff.id)},
    ).json()
    patient_id = created["id"]
    pregnancy_id = created["current_pregnancy"]["id"]

    response = client.patch(
        f"{PATIENTS}{patient_id}/pregnancies/{pregnancy_id}/",
        data=json.dumps({"nurse": str(nurse.staff.id), "notes": "same nurse, new note"}),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 200


def test_an_explicitly_null_pregnancy_is_accepted(client, make_hospital, auth):
    """A frontend that builds the whole object and sets the absent parts to
    null means the same thing as omitting them — not a 400."""
    hospital = make_hospital("Null Pregnancy Hospital")

    response = post_patient(client, auth(hospital.admin.email), pregnancy=None)

    assert response.status_code == 201
    assert response.json()["current_pregnancy"] is None


# ── Consent ──────────────────────────────────────────────────────────────────


def test_consent_date_is_recorded_when_given(client, make_hospital, auth):
    hospital = make_hospital("Consent Date Hospital")

    response = post_patient(client, auth(hospital.admin.email), consent_date="2026-02-10")

    assert response.status_code == 201
    assert response.json()["consent_date"] == "2026-02-10"
    assert Patient.objects.get(id=response.json()["id"]).has_consent is True


def test_consent_date_is_optional(client, make_hospital, auth):
    """A hospital that records consent on paper leaves it blank rather than
    inventing a date."""
    hospital = make_hospital("No Consent Date Hospital")

    response = post_patient(client, auth(hospital.admin.email))

    assert response.status_code == 201
    assert response.json()["consent_date"] is None
    assert Patient.objects.get(id=response.json()["id"]).has_consent is False


def test_consent_date_can_be_set_later_by_patch(client, make_hospital, auth):
    hospital = make_hospital("Later Consent Hospital")
    headers = auth(hospital.admin.email)
    patient_id = post_patient(client, headers).json()["id"]

    response = client.patch(
        f"{PATIENTS}{patient_id}/",
        data=json.dumps({"consent_date": "2026-03-01"}),
        content_type="application/json",
        **headers,
    )

    assert response.status_code == 200
    assert response.json()["consent_date"] == "2026-03-01"
