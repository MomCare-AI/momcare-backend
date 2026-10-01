"""AISummaryTemplate -- a platform-wide, admin-written plain-text template."""

import pytest

from momcare_platform.core.ai.models import AISummaryTemplate

pytestmark = pytest.mark.django_db


def test_a_platform_tier_template_has_no_organization():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Reading First",
        content="Start with the latest reading, then the risk level, then the patient name.",
    )

    assert template.organization is None
    assert template.is_active is False
    assert template.activated_at is None
    assert template.content.startswith("Start with")
