"""activate_instruction_preset()/deactivate_instruction_preset() -- the
transactional "at most one active per scope" swap."""

import pytest

from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.ai.services import (
    InstructionPresetStateError,
    activate_instruction_preset,
    deactivate_instruction_preset,
)

pytestmark = pytest.mark.django_db


def test_activating_a_preset_deactivates_the_previous_one_in_the_same_scope(make_hospital):
    hospital = make_hospital("Activation Swap Hospital")
    old = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Old", content="Old text.", is_active=True,
    )
    new = AIInstructionPreset.objects.create(
        organization=hospital.org, name="New", content="New text.",
    )

    activate_instruction_preset(new)

    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True
    assert new.activated_at is not None


def test_activating_an_organization_preset_never_touches_the_platform_tier(make_hospital):
    hospital = make_hospital("Activation Isolation Hospital")
    platform_preset = AIInstructionPreset.objects.create(
        organization=None, name="Platform", content="Platform text.", is_active=True,
    )
    org_preset = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Org", content="Org text.",
    )

    activate_instruction_preset(org_preset)

    platform_preset.refresh_from_db()
    assert platform_preset.is_active is True


def test_activating_an_organization_preset_never_touches_another_organizations(make_hospital):
    hospital_a = make_hospital("Activation Org A Hospital")
    hospital_b = make_hospital("Activation Org B Hospital")
    preset_a = AIInstructionPreset.objects.create(
        organization=hospital_a.org, name="A", content="A text.", is_active=True,
    )
    preset_b = AIInstructionPreset.objects.create(
        organization=hospital_b.org, name="B", content="B text.",
    )

    activate_instruction_preset(preset_b)

    preset_a.refresh_from_db()
    assert preset_a.is_active is True


def test_activating_an_already_active_preset_raises(make_hospital):
    hospital = make_hospital("Activation Already Active Hospital")
    preset = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Active", content="Text.", is_active=True,
    )

    with pytest.raises(InstructionPresetStateError):
        activate_instruction_preset(preset)


def test_deactivating_clears_is_active_and_keeps_activated_at(make_hospital):
    from django.utils import timezone

    hospital = make_hospital("Deactivation Hospital")
    preset = AIInstructionPreset.objects.create(
        organization=hospital.org,
        name="Active",
        content="Text.",
        is_active=True,
        activated_at=timezone.now(),
    )

    deactivate_instruction_preset(preset)

    preset.refresh_from_db()
    assert preset.is_active is False
    assert preset.activated_at is not None


def test_deactivating_an_already_inactive_preset_raises(make_hospital):
    hospital = make_hospital("Deactivation Already Inactive Hospital")
    preset = AIInstructionPreset.objects.create(
        organization=hospital.org, name="Inactive", content="Text.",
    )

    with pytest.raises(InstructionPresetStateError):
        deactivate_instruction_preset(preset)
