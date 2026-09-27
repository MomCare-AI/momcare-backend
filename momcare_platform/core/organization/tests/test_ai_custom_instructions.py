"""Organization.ai_custom_instructions -- a plain, blank-by-default column,
same shape as confidence_threshold's per-hospital override."""

import pytest

pytestmark = pytest.mark.django_db


def test_ai_custom_instructions_defaults_to_blank(make_hospital):
    hospital = make_hospital("Instructions Default Hospital")

    assert hospital.org.ai_custom_instructions == ""


def test_ai_custom_instructions_can_be_set(make_hospital):
    hospital = make_hospital("Instructions Set Hospital")
    hospital.org.ai_custom_instructions = "Always mention medication adherence if a note references it."
    hospital.org.save(update_fields=["ai_custom_instructions", "updated_at"])

    hospital.org.refresh_from_db()
    assert hospital.org.ai_custom_instructions == "Always mention medication adherence if a note references it."
