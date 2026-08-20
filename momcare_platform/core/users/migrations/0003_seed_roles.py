from django.db import migrations

ROLES = [
    ("platform_admin", "Platform Admin", "MomCare's own operators — not scoped to any single hospital."),
    ("hospital_admin", "Hospital Admin", "Manages a single hospital's staff, locations, and settings."),
    ("provider", "Provider", "Clinical provider delivering care to patients."),
    ("nurse", "Nurse", "Nursing staff supporting patient care within a hospital."),
    ("care_manager", "Care Manager", "Coordinates patient care plans and follow-ups."),
    ("patient", "Patient", "A patient receiving care from a hospital."),
]


def seed_roles(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    for code, name, description in ROLES:
        Role.objects.get_or_create(code=code, defaults={"name": name, "description": description})


def unseed_roles(apps, schema_editor):
    Role = apps.get_model("users", "Role")
    Role.objects.filter(code__in=[code for code, _name, _description in ROLES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_initial'),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
