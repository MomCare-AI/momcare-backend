"""Summary templates are platform-only from 2026-10-01: delete every
hospital-level row and drop the free-text extra_instructions column.

Reversal restores the (blank) column only -- deleted hospital templates and
the wording that lived in extra_instructions are not recoverable, by design.

The DELETE runs under ``app.rls_bypass`` because the table has FORCE ROW
LEVEL SECURITY: without it, a migration role lacking BYPASSRLS would see
zero hospital rows and silently delete nothing.
"""

from django.db import migrations, models


def delete_hospital_templates(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("SELECT set_config('app.rls_bypass', 'on', true)")
    AISummaryTemplate = apps.get_model("ai", "AISummaryTemplate")
    AISummaryTemplate.objects.filter(organization__isnull=False).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0010_aisummary_citations"),
    ]

    operations = [
        migrations.RunPython(delete_hospital_templates, migrations.RunPython.noop),
        migrations.RemoveField(model_name="aisummarytemplate", name="extra_instructions"),
    ]
