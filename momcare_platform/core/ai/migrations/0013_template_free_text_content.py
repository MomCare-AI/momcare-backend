"""Summary templates become plain free text (2026-10-01): ``sections`` (a JSON
layout) is replaced by ``content``, text the platform admin writes in their
own words. Existing templates are converted so their layout survives: each
section becomes "Label: plain description of each field." Forward-only."""

from django.db import migrations, models

_LABELS = {
    "patient_name": "the patient's name",
    "gestational_age": "how far along the pregnancy is (gestational age)",
    "current_risk_level": "the current risk level (low / medium / high)",
    "risk_this_month": "the share of low / medium / high risk this month",
    "latest_readings": "the latest vital readings (blood pressure, heart rate, temperature, glucose, hemoglobin)",
    "thirty_day_average": "the 30-day average of the vitals",
    "provider_name": "the provider (doctor) name",
    "nurse_name": "the nurse's name",
    "care_manager_name": "the care manager's name",
    "recent_note": "the most recent clinical note",
    "recent_note_author": "who wrote the most recent note",
    "last_monitoring_contact_display": "when staff last contacted or monitored her",
    "last_reading_display": "when her last reading was received",
    "monitoring_time_display": "the monitoring time logged this month",
    "active_statuses": "her current statuses",
    "pending_risk_count": "how many risk assessments are waiting for review",
    "has_open_alert": "whether she has an open alert",
}


def sections_to_content(apps, schema_editor):
    AISummaryTemplate = apps.get_model("ai", "AISummaryTemplate")
    for template in AISummaryTemplate.objects.all():
        lines = []
        for section in template.sections or []:
            described = ", ".join(_LABELS.get(f, f) for f in section.get("fields", []))
            lines.append(f"{section.get('label', '')}: {described}.")
        template.content = "\n".join(lines)
        template.save(update_fields=["content"])


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0012_max_words_default_130"),
    ]

    operations = [
        migrations.AddField(
            model_name="aisummarytemplate",
            name="content",
            field=models.TextField(default=""),
            preserve_default=False,
        ),
        migrations.RunPython(sections_to_content, migrations.RunPython.noop),
        migrations.RemoveField(model_name="aisummarytemplate", name="sections"),
    ]
