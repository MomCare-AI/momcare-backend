"""A new Location copies its organization's clinical tags down as
independent rows -- same shape as Neuro_RPM's own
``copy_org_note_templates_to_new_location`` (and its two siblings),
applied here to ClinicalTag, which Neuro_RPM itself never scoped this way.
"""

import pytest

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import ClinicalTag

pytestmark = pytest.mark.django_db


def test_new_location_copies_org_level_tags(make_hospital):
    hospital = make_hospital("Copy Down Hospital")
    ClinicalTag.objects.create(name="High Risk", color="#ff0000", organization=hospital.org)
    ClinicalTag.objects.create(name="Follow Up", organization=hospital.org)

    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)

    copied = ClinicalTag.objects.filter(location=location).order_by("name")
    assert list(copied.values_list("name", "color")) == [("Follow Up", None), ("High Risk", "#ff0000")]


def test_copied_tag_is_independent_of_the_original(make_hospital):
    """Editing the location's copy must never touch the organization's tag."""
    hospital = make_hospital("Independent Copy Hospital")
    org_tag = ClinicalTag.objects.create(name="Watch Closely", organization=hospital.org)

    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    copy = ClinicalTag.objects.get(location=location, name="Watch Closely")

    copy.name = "Renamed Locally"
    copy.save()

    org_tag.refresh_from_db()
    assert org_tag.name == "Watch Closely"


def test_location_scoped_tags_are_not_copied_to_other_locations(make_hospital):
    """Only org-level tags propagate -- a location's own tags stay local."""
    hospital = make_hospital("No Cross Copy Hospital")
    first_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 1",
        location_manager=hospital.admin,
    )
    ClinicalTag.objects.create(name="Branch 1 Only", location=first_location)

    second_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 2",
        location_manager=hospital.admin,
    )

    assert not ClinicalTag.objects.filter(location=second_location, name="Branch 1 Only").exists()


def test_updating_an_existing_location_does_not_recopy_tags(make_hospital):
    """The signal only fires on creation -- a save() on an existing Location
    must not duplicate the org's tags into it again."""
    hospital = make_hospital("No Recopy Hospital")
    ClinicalTag.objects.create(name="Only Once", organization=hospital.org)
    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    assert ClinicalTag.objects.filter(location=location, name="Only Once").count() == 1

    location.name = "Branch 2 Renamed"
    location.save()

    assert ClinicalTag.objects.filter(location=location, name="Only Once").count() == 1


def test_new_locations_at_another_hospital_are_unaffected(make_hospital):
    a = make_hospital("Signal Isolation Hospital A")
    b = make_hospital("Signal Isolation Hospital B")
    ClinicalTag.objects.create(name="A Only", organization=a.org)

    location_b = Location.objects.create(organization=b.org, name="B Branch", location_manager=b.admin)

    assert not ClinicalTag.objects.filter(location=location_b, name="A Only").exists()
