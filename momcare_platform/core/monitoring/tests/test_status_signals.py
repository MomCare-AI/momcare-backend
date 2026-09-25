"""A new Location copies its organization's status labels down as
independent rows -- same shape as ``test_signals.py``'s ClinicalTag
coverage, applied here to StatusLabel.
"""

import pytest

from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import StatusLabel

pytestmark = pytest.mark.django_db


def test_new_location_copies_org_level_status_labels(make_hospital):
    hospital = make_hospital("Status Copy Down Hospital")
    StatusLabel.objects.create(name="Critical", color="#ff0000", organization=hospital.org)
    StatusLabel.objects.create(name="Waiting", organization=hospital.org)

    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)

    copied = StatusLabel.objects.filter(location=location).order_by("name")
    assert list(copied.values_list("name", "color")) == [("Critical", "#ff0000"), ("Waiting", None)]


def test_copied_status_label_is_independent_of_the_original(make_hospital):
    hospital = make_hospital("Independent Status Copy Hospital")
    org_label = StatusLabel.objects.create(name="Stable", organization=hospital.org)

    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    copy = StatusLabel.objects.get(location=location, name="Stable")

    copy.name = "Renamed Locally"
    copy.save()

    org_label.refresh_from_db()
    assert org_label.name == "Stable"


def test_location_scoped_status_labels_are_not_copied_to_other_locations(make_hospital):
    hospital = make_hospital("No Cross Copy Status Hospital")
    first_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 1",
        location_manager=hospital.admin,
    )
    StatusLabel.objects.create(name="Branch 1 Only", location=first_location)

    second_location = Location.objects.create(
        organization=hospital.org,
        name="Branch 2",
        location_manager=hospital.admin,
    )

    assert not StatusLabel.objects.filter(location=second_location, name="Branch 1 Only").exists()


def test_updating_an_existing_location_does_not_recopy_status_labels(make_hospital):
    hospital = make_hospital("No Recopy Status Hospital")
    StatusLabel.objects.create(name="Only Once", organization=hospital.org)
    location = Location.objects.create(organization=hospital.org, name="Branch 2", location_manager=hospital.admin)
    assert StatusLabel.objects.filter(location=location, name="Only Once").count() == 1

    location.name = "Branch 2 Renamed"
    location.save()

    assert StatusLabel.objects.filter(location=location, name="Only Once").count() == 1


def test_new_locations_at_another_hospital_are_unaffected(make_hospital):
    a = make_hospital("Status Signal Isolation Hospital A")
    b = make_hospital("Status Signal Isolation Hospital B")
    StatusLabel.objects.create(name="A Only", organization=a.org)

    location_b = Location.objects.create(organization=b.org, name="B Branch", location_manager=b.admin)

    assert not StatusLabel.objects.filter(location=location_b, name="A Only").exists()
