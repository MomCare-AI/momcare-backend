"""AISummaryTemplate -- a platform-wide structured section ordering. No
free-text extra_instructions (removed 2026-10-01)."""

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
    assert not hasattr(template, "extra_instructions")
    assert template.sections[0]["label"] == "Vitals & Risk"
