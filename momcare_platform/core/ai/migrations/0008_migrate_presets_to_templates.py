"""Copy any existing active AIInstructionPreset's content into the matching
scope's AISummaryTemplate.extra_instructions, before 0009 drops the
AIInstructionPreset table for good. Only the currently-active preset per
scope is migrated -- that's the only one actually affecting live behavior
today; a preset that was already superseded or deactivated isn't live and
migrating it would just be archaeology, not data-loss prevention (same
scope-of-care precedent as 0004_migrate_instructions_to_presets.py, which
only migrated the single value that was actually live at the time).

Two cases per scope:
- An active template already exists: its extra_instructions gets the
  preset's content (it's blank today, since this field didn't exist before
  this exact migration sequence -- nothing to overwrite).
- No active template exists: a new one is created with the same built-in
  default layout _build_prompt() falls back to in code (services.py's
  _VITALS_AND_RISK_FIELDS / _CARE_TEAM_AND_ACTIVITY_FIELDS, plus a leading
  Patient section for patient_name), carrying the preset's content, and
  activated -- so a hospital that had configured a preset but never touched
  templates keeps the exact same effective prompt after this migration as
  before it.

Reversal is deliberately a no-op, matching 0004's own reasoning: templates
are never deleted anywhere else in this feature, and by the time anyone
reverses this migration a template it created may already have been
superseded by a newer one -- deleting it here would be a different kind of
data loss than the one this migration exists to prevent.
"""

from django.db import migrations
from django.utils import timezone

_VITALS_AND_RISK_FIELDS = [
    "gestational_age",
    "current_risk_level",
    "risk_this_month",
    "latest_readings",
    "thirty_day_average",
    "pending_risk_count",
    "has_open_alert",
]
_CARE_TEAM_AND_ACTIVITY_FIELDS = [
    "provider_name",
    "nurse_name",
    "care_manager_name",
    "recent_note",
    "recent_note_author",
    "last_monitoring_contact_display",
    "last_reading_display",
    "monitoring_time_display",
    "active_statuses",
]
_DEFAULT_SECTIONS = [
    {"label": "Patient", "fields": ["patient_name"]},
    {"label": "Vitals & Risk", "fields": _VITALS_AND_RISK_FIELDS},
    {"label": "Care Team & Activity", "fields": _CARE_TEAM_AND_ACTIVITY_FIELDS},
]


def _migrate_scope(AISummaryTemplate, organization, preset):
    template = AISummaryTemplate.objects.filter(organization=organization, is_active=True).first()
    if template is not None:
        template.extra_instructions = preset.content
        template.save(update_fields=["extra_instructions", "updated_at"])
        return

    AISummaryTemplate.objects.create(
        organization=organization,
        name="Migrated template",
        sections=_DEFAULT_SECTIONS,
        extra_instructions=preset.content,
        is_active=True,
        activated_at=timezone.now(),
    )


def _forwards(apps, schema_editor):
    AIInstructionPreset = apps.get_model("ai", "AIInstructionPreset")
    AISummaryTemplate = apps.get_model("ai", "AISummaryTemplate")

    for preset in AIInstructionPreset.objects.filter(is_active=True):
        _migrate_scope(AISummaryTemplate, preset.organization, preset)


def _backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0007_add_extra_instructions_to_summary_template"),
    ]

    operations = [
        migrations.RunPython(_forwards, _backwards),
    ]
