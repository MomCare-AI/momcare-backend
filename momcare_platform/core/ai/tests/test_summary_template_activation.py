"""activate_summary_template()/deactivate_summary_template() -- same
transactional "at most one active per scope" swap as instruction presets,
plus validate_template_sections() enforcing the fixed field vocabulary:
every field exactly once, nothing invented, nothing omitted."""

import pytest

from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_VOCABULARY,
    activate_summary_template,
    deactivate_summary_template,
    validate_template_sections,
)
from momcare_platform.core.ai.services import ActivationStateError as TemplateStateError

pytestmark = pytest.mark.django_db


def _valid_sections():
    """One section per field, in vocabulary order -- valid by construction."""
    return [{"label": "All Fields", "fields": list(TEMPLATE_FIELD_VOCABULARY)}]


def test_activating_a_template_deactivates_the_previous_one_in_the_same_scope():
    old = AISummaryTemplate.objects.create(
        organization=None,
        name="Old",
        sections=_valid_sections(),
        is_active=True,
    )
    new = AISummaryTemplate.objects.create(organization=None, name="New", sections=_valid_sections())

    activate_summary_template(new)

    old.refresh_from_db()
    new.refresh_from_db()
    assert old.is_active is False
    assert new.is_active is True
    assert new.activated_at is not None


def test_activating_an_already_active_template_raises():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        sections=_valid_sections(),
        is_active=True,
    )

    with pytest.raises(TemplateStateError):
        activate_summary_template(template)


def test_deactivating_clears_is_active():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Active",
        sections=_valid_sections(),
        is_active=True,
    )

    deactivate_summary_template(template)

    template.refresh_from_db()
    assert template.is_active is False


def test_deactivating_an_already_inactive_template_raises():
    template = AISummaryTemplate.objects.create(
        organization=None,
        name="Inactive",
        sections=_valid_sections(),
    )

    with pytest.raises(TemplateStateError):
        deactivate_summary_template(template)


def test_valid_sections_covering_every_field_exactly_once_passes():
    errors = validate_template_sections(_valid_sections())

    assert errors == []


def test_an_unknown_field_name_is_rejected():
    sections = [{"label": "Bad", "fields": ["patient_name", "made_up_field"]}]

    errors = validate_template_sections(sections)

    assert any("made_up_field" in e for e in errors)


def test_a_missing_field_is_rejected():
    sections = [{"label": "Incomplete", "fields": [f for f in TEMPLATE_FIELD_VOCABULARY if f != "patient_name"]}]

    errors = validate_template_sections(sections)

    assert any("patient_name" in e and "missing" in e.lower() for e in errors)


def test_a_duplicated_field_across_sections_is_rejected():
    sections = [
        {"label": "A", "fields": list(TEMPLATE_FIELD_VOCABULARY)},
        {"label": "B", "fields": ["patient_name"]},
    ]

    errors = validate_template_sections(sections)

    assert any("patient_name" in e and "once" in e.lower() for e in errors)


def test_a_section_missing_a_label_or_fields_key_is_rejected():
    errors = validate_template_sections([{"fields": list(TEMPLATE_FIELD_VOCABULARY)}])

    assert any("label" in e.lower() for e in errors)
