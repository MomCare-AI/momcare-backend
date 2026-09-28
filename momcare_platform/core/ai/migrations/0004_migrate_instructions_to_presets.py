"""Copy any existing non-empty AIProviderConfig.custom_instructions /
Organization.ai_custom_instructions into a first active AIInstructionPreset
per scope, before 0005/0029 drop those two fields for good. A blank value
(the default at both tiers) creates no preset -- an empty-content preset
would silently outrank "no instructions at all" the moment anyone looked at
the resulting list.

Reversal is deliberately a no-op: presets are never deleted anywhere else in
this feature, and by the time anyone reverses this migration a preset it
created may already have been deactivated or superseded by a newer one --
deleting it here would be a different kind of data loss than the one this
migration exists to prevent.
"""

from django.db import migrations
from django.utils import timezone


def _forwards(apps, schema_editor):
    AIProviderConfig = apps.get_model("ai", "AIProviderConfig")
    AIInstructionPreset = apps.get_model("ai", "AIInstructionPreset")
    Organization = apps.get_model("organization", "Organization")

    config = AIProviderConfig.objects.first()
    if config is not None and config.custom_instructions:
        AIInstructionPreset.objects.create(
            organization=None,
            name="Migrated instructions",
            content=config.custom_instructions,
            is_active=True,
            activated_at=timezone.now(),
        )

    for org in Organization.objects.exclude(ai_custom_instructions=""):
        AIInstructionPreset.objects.create(
            organization=org,
            name="Migrated instructions",
            content=org.ai_custom_instructions,
            is_active=True,
            activated_at=timezone.now(),
        )


def _backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0003_aiinstructionpreset"),
        ("organization", "0026_organization_ai_custom_instructions"),
    ]

    operations = [
        migrations.RunPython(_forwards, _backwards),
    ]
