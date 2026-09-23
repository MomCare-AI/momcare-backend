"""Location service functions, tested directly."""

import pytest

from momcare_platform.core.locations.models import Location
from momcare_platform.core.locations.services import (
    LocationError,
    create_location,
    deactivate_location,
    ensure_default_location,
    move_patients_to_location,
    reactivate_location,
)
from momcare_platform.core.patients.services import onboard_patient

pytestmark = pytest.mark.django_db


def test_the_default_location_is_managed_by_the_owner_from_day_one(make_hospital):
    """Nobody else exists yet at the moment a hospital is approved — the
    owner is the only person there is to name, and a manager is compulsory
    on every location, no exceptions."""
    hospital = make_hospital("Fresh Hospital")

    location = ensure_default_location(hospital.org)

    assert location.location_manager_id == hospital.admin.id


def test_ensure_default_location_is_idempotent(make_hospital):
    hospital = make_hospital("Repeat Hospital")
    first = ensure_default_location(hospital.org)

    second = ensure_default_location(hospital.org)

    assert first.pk == second.pk


def test_create_location_requires_a_manager(make_hospital, make_staff):
    from django.conf import settings

    hospital = make_hospital("New Branch Hospital")
    nurse = make_staff(hospital.org, settings.ROLE_NURSE, "nurse@newbranch.test")

    location = create_location(organization=hospital.org, name="North Wing", location_manager=nurse)

    assert location.location_manager_id == nurse.id
    assert location.organization_id == hospital.org.id


def test_deactivating_is_blocked_while_patients_are_assigned(make_hospital):
    hospital = make_hospital("Occupied Hospital")
    location = ensure_default_location(hospital.org)
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )

    with pytest.raises(LocationError):
        deactivate_location(location)

    location.refresh_from_db()
    assert location.is_active is True


def test_deactivating_succeeds_once_empty(make_hospital):
    hospital = make_hospital("Empty Hospital")
    location = ensure_default_location(hospital.org)

    deactivate_location(location, by=hospital.admin, reason="Closing this branch.")

    location.refresh_from_db()
    assert location.is_active is False
    assert location.deactivated_by_id == hospital.admin.id
    assert location.deactivation_reason == "Closing this branch."


def test_reactivating_restores_it(make_hospital):
    hospital = make_hospital("Reopened Hospital")
    location = ensure_default_location(hospital.org)
    deactivate_location(location)

    reactivate_location(location)

    location.refresh_from_db()
    assert location.is_active is True
    assert location.deactivated_at is None


def test_move_all_patients_to_another_location(make_hospital):
    hospital = make_hospital("Moving Hospital")
    source = ensure_default_location(hospital.org)
    target = Location.objects.create(
        organization=hospital.org,
        name="Second Branch",
        location_manager=hospital.admin,
    )
    onboard_patient(
        organization=hospital.org,
        patient_data={"first_name": "Ayesha", "last_name": "Bibi"},
    )

    moved = move_patients_to_location(source, target, move_all=True)

    assert moved == 1
    assert source.patients.filter(is_active=True).count() == 0
    assert target.patients.count() == 1


def test_moving_to_the_same_location_is_refused(make_hospital):
    hospital = make_hospital("Same Location Hospital")
    location = ensure_default_location(hospital.org)

    with pytest.raises(LocationError):
        move_patients_to_location(location, location, move_all=True)


def test_moving_specific_patients_requires_them_to_actually_be_there(make_hospital):
    hospital = make_hospital("Mismatch Hospital")
    source = ensure_default_location(hospital.org)
    target = Location.objects.create(organization=hospital.org, name="Elsewhere", location_manager=hospital.admin)

    with pytest.raises(LocationError):
        move_patients_to_location(source, target, move_all=False, patient_ids=["00000000-0000-0000-0000-000000000000"])
