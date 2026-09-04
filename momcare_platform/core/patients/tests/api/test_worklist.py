"""The worklist — administrative/care-continuity gaps, deliberately a
different question from the Attention Queue's clinical severity.

Every reason is checked independently: absent when the underlying condition
doesn't hold, present with the right detail when it does, and a pregnancy
with no gaps at all never appears in the results — mirroring the empty-state
discipline the rest of this codebase applies everywhere else.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from momcare_platform.core.monitoring.models import VitalReading
from momcare_platform.core.patients.models import ClinicalNote, Consent, Pregnancy, PregnancyRiskFactors
from momcare_platform.core.patients.services import enrol_patient

pytestmark = pytest.mark.django_db

WORKLIST = "/api/patients/worklist/"


@pytest.fixture
def pregnancy_for(db, make_staff):
    """Enrol a fully-answered, freshly-monitored, clinician-assigned pregnancy
    — the "clean" baseline every test starts from and then breaks one
    condition at a time, so each reason is proven both present and absent.

    A note always needs a real author, independent of whether the test
    cares about a *lead* clinician being assigned, so this always creates
    one rather than reusing ``assigned_staff`` (which callers may pass as
    None on purpose, to test the no-lead-clinician reason).
    """

    def _make(hospital, *, first_name="Ayesha", assigned_staff=None):
        author = make_staff(
            hospital.org,
            settings.ROLE_NURSE,
            f"note-author-{hospital.org.id}@fixture.test",
        )
        patient = enrol_patient(
            organization=hospital.org,
            recorded_by=hospital.admin,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={
                "lmp": timezone.now().date() - timedelta(weeks=28),
                "assigned_staff": assigned_staff.staff if assigned_staff else None,
            },
            risk_factor_data={"chronic_hypertension": PregnancyRiskFactors.YES},
            consent={"status": Consent.STATUS_GRANTED},
        )
        pregnancy = patient.current_pregnancy
        VitalReading.objects.create(
            pregnancy=pregnancy,
            reading_type=VitalReading.TYPE_HEART_RATE,
            value=80,
            recorded_at=timezone.now(),
            source=VitalReading.SOURCE_MANUAL,
        )
        ClinicalNote.objects.create(
            pregnancy=pregnancy,
            author=author.staff,
            body="Routine check, all normal.",
        )
        return pregnancy

    return _make


def reasons_for(response_json, pregnancy_id) -> list[str]:
    for row in response_json["results"]:
        if row["pregnancy_id"] == str(pregnancy_id):
            return [r["code"] for r in row["reasons"]]
    return []


def test_a_pregnancy_with_no_gaps_never_appears(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Clean Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@cleanhospital.test")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": timezone.now().date() - timedelta(weeks=28),
            "assigned_staff": doctor.staff,
        },
        risk_factor_data={"chronic_hypertension": PregnancyRiskFactors.YES},
        consent={"status": Consent.STATUS_GRANTED},
    )
    pregnancy = patient.current_pregnancy
    VitalReading.objects.create(
        pregnancy=pregnancy,
        reading_type=VitalReading.TYPE_HEART_RATE,
        value=80,
        recorded_at=timezone.now(),
        source=VitalReading.SOURCE_MANUAL,
    )
    ClinicalNote.objects.create(pregnancy=pregnancy, author=doctor.staff, body="All normal.")

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    assert response.status_code == 200, response.content
    assert response.json()["count"] == 0


def test_no_reading_ever_recorded(client, make_hospital, auth):
    hospital = make_hospital("No Reading Hospital")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Zainab", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        consent={"status": Consent.STATUS_GRANTED},
    )
    pregnancy = patient.current_pregnancy

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_recent_reading" in codes
    row = next(r for r in response.json()["results"] if r["pregnancy_id"] == str(pregnancy.id))
    reading_reason = next(r for r in row["reasons"] if r["code"] == "no_recent_reading")
    assert reading_reason["days"] is None
    assert "ever" in reading_reason["detail"]


def test_a_stale_reading_beyond_seven_days_is_flagged(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Stale Reading Hospital")
    pregnancy = pregnancy_for(hospital)
    # The "clean" fixture already has a fresh reading — replace it with an old one.
    VitalReading.objects.filter(pregnancy=pregnancy).delete()
    VitalReading.objects.create(
        pregnancy=pregnancy,
        reading_type=VitalReading.TYPE_HEART_RATE,
        value=80,
        recorded_at=timezone.now() - timedelta(days=10),
        source=VitalReading.SOURCE_MANUAL,
    )

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_recent_reading" in codes


def test_a_recent_reading_clears_the_reason(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Recent Reading Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_recent_reading" not in codes


def test_no_note_in_thirty_days_is_flagged(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Stale Note Hospital")
    pregnancy = pregnancy_for(hospital)
    ClinicalNote.objects.filter(pregnancy=pregnancy).update(
        created_at=timezone.now() - timedelta(days=45),
    )

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_recent_note" in codes


def test_a_recent_note_clears_the_reason(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Recent Note Hospital")
    pregnancy = pregnancy_for(hospital)

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_recent_note" not in codes


def test_risk_history_entirely_unanswered_is_flagged(client, make_hospital, auth):
    hospital = make_hospital("No Risk History Hospital")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Fatima", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        # No risk_factor_data - every field defaults to "unknown".
        consent={"status": Consent.STATUS_GRANTED},
    )
    pregnancy = patient.current_pregnancy

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_risk_history" in codes


def test_one_answered_risk_factor_clears_the_reason(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("Answered Risk Hospital")
    pregnancy = pregnancy_for(hospital)  # fixture answers chronic_hypertension

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_risk_history" not in codes


def test_a_pregnancy_with_no_risk_factors_row_at_all_is_flagged(client, make_hospital, auth):
    """Defensive: create_pregnancy always creates the row, but a pregnancy
    reached some other way (direct ORM, a future code path) must not crash
    or be silently treated as answered."""
    hospital = make_hospital("Bypassed Enrolment Hospital")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Hina", "last_name": "Bibi"},
        consent={"status": Consent.STATUS_GRANTED},
    )
    pregnancy = Pregnancy.objects.create(
        patient=patient,
        lmp=timezone.now().date() - timedelta(weeks=20),
    )

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    assert response.status_code == 200, response.content
    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_risk_history" in codes


def test_no_lead_clinician_is_flagged(client, make_hospital, pregnancy_for, auth):
    hospital = make_hospital("No Clinician Hospital")
    pregnancy = pregnancy_for(hospital, assigned_staff=None)

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_lead_clinician" in codes


def test_an_inactive_lead_clinician_still_counts_as_unaccountable(
    client, make_hospital, make_staff, pregnancy_for, auth,
):
    hospital = make_hospital("Departed Clinician Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@departed.test")
    pregnancy = pregnancy_for(hospital, assigned_staff=doctor)
    doctor.staff.deactivate()

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_lead_clinician" in codes


def test_an_active_lead_clinician_clears_the_reason(client, make_hospital, make_staff, pregnancy_for, auth):
    hospital = make_hospital("Has Clinician Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@hasclinician.test")
    pregnancy = pregnancy_for(hospital, assigned_staff=doctor)

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    codes = reasons_for(response.json(), pregnancy.id)
    assert "no_lead_clinician" not in codes


def test_a_delivered_pregnancy_never_appears(client, make_hospital, auth):
    hospital = make_hospital("Delivered Hospital")
    patient = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Sana", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=39)},
        consent={"status": Consent.STATUS_GRANTED},
    )
    pregnancy = patient.current_pregnancy
    pregnancy.status = Pregnancy.STATUS_DELIVERED
    pregnancy.save(update_fields=["status", "updated_at"])

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    assert response.json()["count"] == 0


def test_rows_with_more_gaps_sort_first(client, make_hospital, make_staff, auth):
    hospital = make_hospital("Sort Order Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@sortorder.test")

    # One gap: no reading. Risk history answered, note logged, clinician assigned.
    one_gap = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "OneGap", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": timezone.now().date() - timedelta(weeks=20),
            "assigned_staff": doctor.staff,
        },
        risk_factor_data={"chronic_hypertension": PregnancyRiskFactors.NO},
        consent={"status": Consent.STATUS_GRANTED},
    ).current_pregnancy
    ClinicalNote.objects.create(pregnancy=one_gap, author=doctor.staff, body="Note.")

    # Four gaps: no reading, no note, no risk history, no clinician.
    four_gaps = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "FourGaps", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        consent={"status": Consent.STATUS_GRANTED},
    ).current_pregnancy

    response = client.get(WORKLIST, **auth(hospital.admin.email))
    results = response.json()["results"]
    ids_in_order = [r["pregnancy_id"] for r in results]

    assert ids_in_order.index(str(four_gaps.id)) < ids_in_order.index(str(one_gap.id))


# ── Scoping ───────────────────────────────────────────────────────────────


def test_assigned_to_me_scopes_a_provider_to_their_own_cases(
    client, make_hospital, make_staff, auth,
):
    hospital = make_hospital("Scoped Worklist Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@scopedworklist.test")
    other_doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "other@scopedworklist.test")

    mine = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Mine", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": timezone.now().date() - timedelta(weeks=20),
            "assigned_staff": doctor.staff,
        },
        consent={"status": Consent.STATUS_GRANTED},
    ).current_pregnancy
    not_mine = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "NotMine", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": timezone.now().date() - timedelta(weeks=20),
            "assigned_staff": other_doctor.staff,
        },
        consent={"status": Consent.STATUS_GRANTED},
    ).current_pregnancy

    response = client.get(f"{WORKLIST}?assigned_to=me", **auth(doctor.email))

    ids = [r["pregnancy_id"] for r in response.json()["results"]]
    assert str(mine.id) in ids
    assert str(not_mine.id) not in ids


def test_hospital_admin_sees_the_full_unfiltered_worklist(
    client, make_hospital, make_staff, auth,
):
    hospital = make_hospital("Admin Worklist Hospital")
    doctor = make_staff(hospital.org, settings.ROLE_PROVIDER, "doctor@adminworklist.test")
    pregnancy = enrol_patient(
        organization=hospital.org,
        recorded_by=hospital.admin,
        patient_data={"first_name": "Someone", "last_name": "Bibi"},
        pregnancy_data={
            "lmp": timezone.now().date() - timedelta(weeks=20),
            "assigned_staff": doctor.staff,
        },
        consent={"status": Consent.STATUS_GRANTED},
    ).current_pregnancy

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    ids = [r["pregnancy_id"] for r in response.json()["results"]]
    assert str(pregnancy.id) in ids


def test_another_hospitals_pregnancy_never_appears(client, make_hospital, auth):
    hospital = make_hospital("Isolated Hospital A")
    other = make_hospital("Isolated Hospital B")
    other_pregnancy = enrol_patient(
        organization=other.org,
        recorded_by=other.admin,
        patient_data={"first_name": "Elsewhere", "last_name": "Bibi"},
        pregnancy_data={"lmp": timezone.now().date() - timedelta(weeks=20)},
        consent={"status": Consent.STATUS_GRANTED},
    ).current_pregnancy

    response = client.get(WORKLIST, **auth(hospital.admin.email))

    ids = [r["pregnancy_id"] for r in response.json()["results"]]
    assert str(other_pregnancy.id) not in ids
