"""Drop AIInstructionPreset for good -- its job is now
AISummaryTemplate.extra_instructions (added 0007, backfilled by 0008). See
docs/design/2026-09-29-ai-summary-template-merge-design.md.

No explicit RLS-policy-drop step needed first: DROP TABLE removes every
policy defined on that table automatically in Postgres, so DeleteModel's
own DROP TABLE is sufficient. Depends explicitly on organization's RLS
migrations for this table (0028/0030) via the ai<->organization app
ordering already established by those migrations themselves, so a fresh
database always creates the table and its policies before this migration
removes them together.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0008_migrate_presets_to_templates"),
        ("organization", "0031_ai_summary_template_row_level_security"),
    ]

    operations = [
        migrations.DeleteModel(
            name="AIInstructionPreset",
        ),
    ]
