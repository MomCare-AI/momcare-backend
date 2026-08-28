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
from momcare_platform.core.patients.models import Consent, Patient, Pregnancy, PregnancyRiskFactors
from momcare_platform.core.patients.services import enrol_patient
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
        "consent": {"status": "granted", "version": "v1.0", "method": "in_person"},
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


def test_enrolment_records_consent(client, make_hospital, auth):
    hospital = make_hospital("Consent Hospital")
    response = post_patient(client, auth(hospital.admin.email))

    consent = Patient.objects.get(id=response.json()["id"]).consents.first()
    assert consent.status == Consent.STATUS_GRANTED
    assert consent.version == "v1.0"
    assert consent.recorded_by == hospital.admin


def test_enrolment_requires_consent(client, make_hospital, auth):
    """Storing a patient's record without a recorded agreement is refused."""
    hospital = make_hospital("No Consent Hospital")
    payload = enrolment_payload()
    payload.pop("consent")

    response = client.post(
        PATIENTS,
        data=json.dumps(payload),
        content_type="application/json",
        **auth(hospital.admin.email),
    )

    assert response.status_code == 400
    assert "consent" in response.json()
    assert not Patient.objects.exists()


def test_mrn_is_hospital_prefixed_and_sequential(client, make_hospital, auth):
    hospital = make_hospital("Alpha Care")
    headers = auth(hospital.admin.email)

    first = post_patient(client, headers, first_name="One").json()["mrn"]
    second = post_patient(client, headers, first_name="Two", cnic="").json()["mrn"]

    assert first == "ALPH-000001"
    assert second == "ALPH-000002"


def test_mrn_is_unique_across_hospitals(client, make_hospital, auth):
    alpha = make_hospital("Alpha MRN")
    beta = make_hospital("Beta MRN")

    a = post_patient(client, auth(alpha.admin.email)).json()["mrn"]
    b = post_patient(client, auth(beta.admin.email)).json()["mrn"]

    assert a != b
    assert Patient.objects.filter(mrn=a).count() == 1


def test_a_patient_survives_deletion_of_her_user_account(make_hospital):
    """SET_NULL, not CASCADE — losing an app account must not erase a record."""
    hospital = make_hospital("SetNull Hospital")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Zara", "last_name": "Khan"},
        consent={"status": Consent.STATUS_GRANTED},
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
    enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "NoAccount"},
        consent={"status": Consent.STATUS_GRANTED},
    )

    assert hospital.org.patient_count == 1


def test_patient_count_includes_patients_with_a_user(make_hospital):
    hospital = make_hospital("Count With User")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "WithAccount"},
        consent={"status": Consent.STATUS_GRANTED},
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

    factors = response.json()["current_pregnancy"]["risk_factors"]
    for field in PregnancyRiskFactors.FACTOR_FIELDS:
        assert factors[field] == PregnancyRiskFactors.UNKNOWN
    assert factors["present_factors"] == []
    assert len(factors["unanswered_factors"]) == len(PregnancyRiskFactors.FACTOR_FIELDS)


def test_risk_factors_are_recorded_when_given(client, make_hospital, auth):
    hospital = make_hospital("Factors Hospital")

    response = post_patient(
        client,
        auth(hospital.admin.email),
        pregnancy={
            "lmp": date(2026, 2, 5).isoformat(),
            "risk_factors": {"previous_c_section": "yes", "diabetes": "no"},
        },
    )

    factors = response.json()["current_pregnancy"]["risk_factors"]
    assert factors["previous_c_section"] == "yes"
    assert factors["diabetes"] == "no"
    assert factors["previous_preeclampsia"] == "unknown"
    assert factors["present_factors"] == ["previous_c_section"]


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

    listing = client.get(f"{PATIENTS}{patient_id}/pregnancies/", **headers).json()
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
            "assigned_staff": str(doctor.staff.id),
        },
    )

    pregnancy = response.json()["current_pregnancy"]
    assert pregnancy["assigned_staff"] == str(doctor.staff.id)
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
            "assigned_staff": str(beta_doctor.staff.id),
        },
    )

    assert response.status_code == 400
    errors = response.json()["pregnancy"]["assigned_staff"]
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
        data=json.dumps({"assigned_staff": str(beta_doctor.staff.id)}),
        content_type="application/json",
        **headers,
    )

    assert response.status_code == 400
    pregnancy.refresh_from_db()
    assert pregnancy.assigned_staff is None


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
            "assigned_staff": str(doctor.staff.id),
        },
    ).json()["id"]

    doctor.staff.deactivate(reason="Left the hospital")

    detail = client.get(f"{PATIENTS}{patient_id}/", **headers).json()
    pregnancy = detail["current_pregnancy"]
    assert pregnancy["assigned_staff"] is not None, "the historical assignment must be kept"
    assert pregnancy["has_responsible_clinician"] is False, "an inactive clinician must not count"


# ── Consent history ──────────────────────────────────────────────────────────


def test_consent_can_be_withdrawn_without_losing_the_original(client, make_hospital, auth):
    hospital = make_hospital("Withdraw Hospital")
    headers = auth(hospital.admin.email)
    patient_id = post_patient(client, headers).json()["id"]

    response = client.post(
        f"{PATIENTS}{patient_id}/consent/",
        data=json.dumps({"status": "withdrawn", "note": "Patient asked to stop monitoring."}),
        content_type="application/json",
        **headers,
    )

    assert response.status_code == 201
    history = Patient.objects.get(id=patient_id).consents.all()
    assert [c.status for c in history] == ["withdrawn", "granted"]


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
    mrn = post_patient(client, headers).json()["mrn"]

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
    from django.utils import timezone  # noqa: PLC0415

    from momcare_platform.core.monitoring.models import VitalReading  # noqa: PLC0415
    from momcare_platform.core.monitoring.services import reassess_risk  # noqa: PLC0415

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
        reading_type=VitalReading.TYPE_BLOOD_PRESSURE,
        value=168,
        value_secondary=112,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
    )
    reassess_risk(pregnancy)

    row = client.get(PATIENTS, **auth(hospital.admin.email)).json()["results"][0]

    assert row["risk_level"] == "critical"
    assert row["risk_assessed_at"] is not None
    assert row["pregnancy_id"] == str(pregnancy.id)


def test_a_patient_never_assessed_reports_no_level_rather_than_stable(
    client, make_hospital, auth,
):
    """Absent is not the same as safe. Reporting "stable" for someone nobody has
    measured would be the system inventing reassurance it has no basis for."""
    hospital = make_hospital("Unassessed Hospital")
    post_patient(client, auth(hospital.admin.email))

    row = client.get(PATIENTS, **auth(hospital.admin.email)).json()["results"][0]

    assert row["risk_level"] is None
    assert row["risk_assessed_at"] is None


def test_listing_more_patients_does_not_cost_more_queries(
    client, make_hospital, auth, django_assert_max_num_queries,
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
