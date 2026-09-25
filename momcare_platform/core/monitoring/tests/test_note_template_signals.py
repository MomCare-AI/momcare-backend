"""A new Location copies its organization's note templates down as
independent rows -- same shape as ``test_status_signals.py``'s StatusLabel
coverage, applied here to NoteTemplate. Confirmed to match Neuro_RPM's own
real mechanism for NoteTemplate (a signal, not a plain function call).
"""

import pytest

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import NoteTemplate

pytestmark = pytest.mark.django_db


def test_new_location_copies_org_level_note_templates(make_hospital):
    hospital = make_hospital("Template Copy Down Hospital")
    NoteTemplate.objects.create(
        title="Routine Check-in",
        content="Patient reports feeling well.",
        organization=hospital.org,
    )
    NoteTemplate.objects.create(
        title="Missed Appointment", content="Attempted contact, no answer.", organization=hospital.org
    )

    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)

    copied = NoteTemplate.objects.filter(location=location).order_by("title")
    assert list(copied.values_list("title", "content")) == [
        ("Missed Appointment", "Attempted contact, no answer."),
        ("Routine Check-in", "Patient reports feeling well."),
    ]


def test_copied_note_template_is_independent_of_the_original(make_hospital):
    hospital = make_hospital("Independent Template Copy Hospital")
    org_template = NoteTemplate.objects.create(
        title="Routine Check-in", content="Original text.", organization=hospital.org
    )

    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    copy = NoteTemplate.objects.get(location=location, title="Routine Check-in")

    copy.content = "Edited locally."
    copy.save()

    org_template.refresh_from_db()
    assert org_template.content == "Original text."


def test_location_scoped_note_templates_are_not_copied_to_other_locations(make_hospital):
    hospital = make_hospital("No Cross Copy Template Hospital")
    first_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 1",
        location_manager=hospital.admin,
    )
    NoteTemplate.objects.create(title="Branch 1 Only", content="x", location=first_location)

    second_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 2",
        location_manager=hospital.admin,
    )

    assert not NoteTemplate.objects.filter(location=second_location, title="Branch 1 Only").exists()


def test_updating_an_existing_location_does_not_recopy_note_templates(make_hospital):
    hospital = make_hospital("No Recopy Template Hospital")
    NoteTemplate.objects.create(title="Only Once", content="x", organization=hospital.org)
    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    assert NoteTemplate.objects.filter(location=location, title="Only Once").count() == 1

    location.name = "Branch 2 Renamed"
    location.save()

    assert NoteTemplate.objects.filter(location=location, title="Only Once").count() == 1


def test_new_locations_at_another_hospital_are_unaffected(make_hospital):
    a = make_hospital("Template Signal Isolation Hospital A")
    b = make_hospital("Template Signal Isolation Hospital B")
    NoteTemplate.objects.create(title="A Only", content="x", organization=a.org)

    location_b = Location.objects.create(organization=b.org, name="B Branch", location_manager=b.admin)

    assert not NoteTemplate.objects.filter(location=location_b, title="A Only").exists()


def test_a_note_template_created_after_the_location_already_exists_is_not_retroactively_copied(make_hospital):
    """The signal only fires on Location creation -- a template added to the
    org later never backfills into locations that already exist."""
    hospital = make_hospital("No Retroactive Copy Hospital")
    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)

    NoteTemplate.objects.create(title="Added Later", content="x", organization=hospital.org)

    assert not NoteTemplate.objects.filter(location=location, title="Added Later").exists()
