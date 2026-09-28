"""AIInstructionPreset -- organization nullable doubles as the tier marker
(None = platform-wide, set = that hospital's own)."""

import pytest

from momcare_platform.core.ai.models import AIInstructionPreset

pytestmark = pytest.mark.django_db


def test_a_platform_tier_preset_has_no_organization(make_hospital):
    preset = AIInstructionPreset.objects.create(
        organization=None,
        name="Baseline",
        content="Always mention medication adherence.",
    )

    assert preset.organization is None
    assert preset.is_active is False
    assert preset.activated_at is None


def test_an_organization_tier_preset_carries_its_hospital(make_hospital):
    hospital = make_hospital("Preset Model Hospital")

    preset = AIInstructionPreset.objects.create(
        organization=hospital.org,
        name="Winter 2026",
        content="Emphasize hydration.",
    )

    assert preset.organization_id == hospital.org.id
