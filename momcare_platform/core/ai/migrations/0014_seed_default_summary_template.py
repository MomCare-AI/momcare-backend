"""The default summary layout used to live in code (services.py). It is now a
real platform-level AISummaryTemplate row, so it can be read, copied and
replaced through the platform-admin API like any other template.

The new row is made active only if no platform template is active yet -- a
deployment whose admin already activated their own keeps it. Reversal removes
the seeded row by name.

Runs under ``app.rls_bypass`` because the table has FORCE ROW LEVEL SECURITY
and the migration role may lack BYPASSRLS."""

from django.db import migrations
from django.utils import timezone

NAME = "Default Summary Template"

CONTENT = (
    "Begin with the patient's name and how far along her pregnancy is. "
    "Then write a paragraph on her vitals and risk: her current risk level and how her risk has split "
    "between low, medium and high this month, her latest vital readings (blood pressure, heart rate, "
    "temperature, glucose, hemoglobin) and their 30-day average, how many risk assessments are waiting "
    "for review, and whether she has an open alert. "
    "Then write a second paragraph on her care team and activity: her provider, nurse and care manager, "
    "the most recent clinical note and who wrote it, when staff last contacted her, when her last reading "
    "was received, her monitoring time this month, and her current statuses."
)


def _bypass(schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("SELECT set_config('app.rls_bypass', 'on', true)")


def seed(apps, schema_editor):
    _bypass(schema_editor)
    AISummaryTemplate = apps.get_model("ai", "AISummaryTemplate")
    if AISummaryTemplate.objects.filter(organization__isnull=True, name=NAME).exists():
        return
    nothing_active = not AISummaryTemplate.objects.filter(organization__isnull=True, is_active=True).exists()
    AISummaryTemplate.objects.create(
        organization=None,
        name=NAME,
        content=CONTENT,
        is_active=nothing_active,
        activated_at=timezone.now() if nothing_active else None,
    )


def unseed(apps, schema_editor):
    _bypass(schema_editor)
    apps.get_model("ai", "AISummaryTemplate").objects.filter(organization__isnull=True, name=NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0013_template_free_text_content"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
