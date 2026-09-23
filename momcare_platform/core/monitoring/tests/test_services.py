"""``create_combined_monitoring`` and ``get_or_create_tags`` -- the
transactional core every monitoring endpoint writes through.
"""

import pytest
from django.utils import timezone
from rest_framework import serializers

from momcare_platform.core.monitoring.models import ClinicalTag, MonitoringNote, MonitoringSession
from momcare_platform.core.monitoring.services import create_combined_monitoring, get_or_create_tags
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_for(make_hospital):
    def _make(hospital, first_name="Ayesha", *, with_pregnancy=True):
        return onboard_patient(
            organization=hospital.org,
            patient_data={"first_name": first_name, "last_name": "Bibi"},
            pregnancy_data={"lmp": timezone.now().date()} if with_pregnancy else None,
        )

    return _make


# ── create_combined_monitoring ───────────────────────────────────────────────


def test_duration_only_creates_session_no_note(make_hospital, patient_for):
    hospital = make_hospital("Duration Only Hospital")
    patient = patient_for(hospital)

    session, note = create_combined_monitoring(
        patient=patient,
        duration_seconds=600,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
    )

    assert session is not None
    assert session.pregnancy_id == patient.current_pregnancy.id
    assert note is None


def test_note_only_creates_standalone_note_no_session(make_hospital, patient_for):
    hospital = make_hospital("Note Only Hospital")
    patient = patient_for(hospital)

    session, note = create_combined_monitoring(
        patient=patient,
        duration_seconds=None,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
        note_text="Called to check on swelling, advised to rest.",
    )

    assert session is None
    assert note is not None
    assert note.session is None
    assert note.pregnancy_id == patient.current_pregnancy.id


def test_duration_and_note_link_the_note_to_the_session(make_hospital, patient_for):
    hospital = make_hospital("Both Duration Note Hospital")
    patient = patient_for(hospital)

    session, note = create_combined_monitoring(
        patient=patient,
        duration_seconds=420,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
        note_text="Discussed medication adherence.",
    )

    assert session is not None
    assert note is not None
    assert note.session_id == session.id


def test_neither_duration_nor_note_is_rejected(make_hospital, patient_for):
    hospital = make_hospital("Nothing Provided Hospital")
    patient = patient_for(hospital)

    with pytest.raises(serializers.ValidationError):
        create_combined_monitoring(
            patient=patient,
            duration_seconds=None,
            recorded_at=timezone.now(),
            added_by=hospital.admin,
        )


def test_pregnancy_auto_filled_from_current_pregnancy(make_hospital, patient_for):
    hospital = make_hospital("Auto Fill Hospital")
    patient = patient_for(hospital)
    expected = patient.current_pregnancy

    _session, note = create_combined_monitoring(
        patient=patient,
        duration_seconds=None,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
        note_text="Note about her current pregnancy.",
    )

    assert note is not None
    assert note.pregnancy_id == expected.id


def test_no_pregnancy_yet_still_creates_the_note(make_hospital, patient_for):
    hospital = make_hospital("No Episode Hospital")
    patient = patient_for(hospital, with_pregnancy=False)
    assert patient.current_pregnancy is None

    session, note = create_combined_monitoring(
        patient=patient,
        duration_seconds=120,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
        note_text="Reminded her to bring pregnancy confirmation next visit.",
    )

    assert session is not None
    assert note is not None
    assert session.pregnancy is None
    assert note.pregnancy is None


def test_nothing_persisted_when_note_creation_would_fail_mid_transaction(make_hospital, patient_for, monkeypatch):
    """Atomicity: if the note half fails, the session half must not survive either."""
    hospital = make_hospital("Atomic Rollback Hospital")
    patient = patient_for(hospital)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(MonitoringNote.objects, "create", _boom)

    with pytest.raises(RuntimeError):
        create_combined_monitoring(
            patient=patient,
            duration_seconds=300,
            recorded_at=timezone.now(),
            added_by=hospital.admin,
            note_text="This note will fail to save.",
        )

    assert MonitoringSession.objects.filter(patient=patient).count() == 0


def test_tags_are_attached_via_get_or_create(make_hospital, patient_for):
    hospital = make_hospital("Tag Attach Hospital")
    patient = patient_for(hospital)

    _session, note = create_combined_monitoring(
        patient=patient,
        duration_seconds=None,
        recorded_at=timezone.now(),
        added_by=hospital.admin,
        note_text="Flagged for follow up.",
        tags=[{"name": "Follow Up", "color": "#ff0000"}],
    )

    assert note is not None
    assert list(note.tags.values_list("name", flat=True)) == ["Follow Up"]
    assert ClinicalTag.objects.get(name="Follow Up").location_id == patient.location_id


# ── get_or_create_tags ───────────────────────────────────────────────────────


def test_get_or_create_tags_reuses_existing_name_case_insensitively(make_hospital, patient_for):
    hospital = make_hospital("Case Insensitive Tag Hospital")
    patient = patient_for(hospital)
    existing = ClinicalTag.objects.create(name="Alert", location=patient.location)

    tags = get_or_create_tags([{"name": "  ALERT  "}], location=patient.location)

    assert tags == [existing]
    assert ClinicalTag.objects.filter(name__iexact="alert").count() == 1


def test_get_or_create_tags_new_name_creates_location_scoped_tag(make_hospital, patient_for):
    hospital = make_hospital("New Tag Hospital")
    patient = patient_for(hospital)

    tags = get_or_create_tags([{"name": "Brand New Tag"}], location=patient.location)

    assert len(tags) == 1
    assert tags[0].organization_id is None
    assert tags[0].location_id == patient.location_id


def test_get_or_create_tags_sees_org_level_tags(make_hospital, patient_for):
    hospital = make_hospital("Org Visible Tag Hospital")
    patient = patient_for(hospital)
    org_tag = ClinicalTag.objects.create(name="Hospital Wide", organization=hospital.org)

    tags = get_or_create_tags([{"id": str(org_tag.id)}], location=patient.location)

    assert tags == [org_tag]


def test_get_or_create_tags_rejects_id_from_another_hospital(make_hospital, patient_for):
    a = make_hospital("Isolation Tag Hospital A")
    b = make_hospital("Isolation Tag Hospital B")
    patient_a = patient_for(a)
    foreign_tag = ClinicalTag.objects.create(name="Not Yours", organization=b.org)

    with pytest.raises(serializers.ValidationError):
        get_or_create_tags([{"id": str(foreign_tag.id)}], location=patient_a.location)


def test_get_or_create_tags_rejects_id_from_another_location_same_hospital(make_hospital, patient_for):
    hospital = make_hospital("Cross Location Tag Hospital")
    patient = patient_for(hospital)
    from momcare_platform.core.locations.models import Location  # noqa: PLC0415

    other_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 2",
        location_manager=hospital.admin,
    )
    other_tag = ClinicalTag.objects.create(name="Branch Only", location=other_location)

    with pytest.raises(serializers.ValidationError):
        get_or_create_tags([{"id": str(other_tag.id)}], location=patient.location)
