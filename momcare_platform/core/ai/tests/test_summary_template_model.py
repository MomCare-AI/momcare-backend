"""AISummaryTemplate -- same tier shape as AIInstructionPreset (organization
nullable = platform tier when null), but stores a structured section
ordering instead of free text."""

import pytest

from momcare_platform.core.ai.models import AISummaryTemplate

pytestmark = pytest.mark.django_db


def test_a_platform_tier_template_has_no_organization(make_hospital):
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="RPM Style",
        sections=[
            {"label": "Vitals & Risk", "fields": ["current_risk_level", "latest_readings"]},
            {"label": "Care Team & Activity", "fields": ["provider_name"]},
        ],
    )

    assert template.organization is None
    assert template.is_active is False
    assert template.activated_at is None
    assert template.sections[0]["label"] == "Vitals & Risk"


def test_an_organization_tier_template_carries_its_hospital(make_hospital):
    hospital = make_hospital("Template Model Hospital")

    template = AISummaryTemplate.objects.create(
        organization=hospital.org,
        name="Risk First",
        sections=[{"label": "Risk", "fields": ["current_risk_level"]}],
    )

    assert template.organization_id == hospital.org.id
