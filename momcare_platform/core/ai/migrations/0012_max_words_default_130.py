"""AI Summary word cap: 150 -> 130 (2026-10-01).

The cap lives in a singleton AIProviderConfig row created from settings on
first read, so changing the model/settings default alone would leave every
existing deployment on 150. The data step moves a row still on the old
default to 130; a row an admin deliberately set to any other value is left
alone. Reversal is a no-op for the data (can't tell old-default from a
deliberate 130)."""

from django.db import migrations, models


def lower_old_default(apps, schema_editor):
    AIProviderConfig = apps.get_model("ai", "AIProviderConfig")
    AIProviderConfig.objects.filter(max_words=150).update(max_words=130)


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0011_platform_only_summary_templates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aiproviderconfig",
            name="max_words",
            field=models.PositiveIntegerField(default=130),
        ),
        migrations.RunPython(lower_old_default, migrations.RunPython.noop),
    ]
