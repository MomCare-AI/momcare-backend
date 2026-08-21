"""Give every approved hospital a location to admit patients to.

Patients belong to a Location, never directly to an Organization, so a hospital
with no location cannot enrol anyone. Approval creates one from now on; this
backfills the hospitals approved before that existed.

Only approved-and-active hospitals get one — a rejected application has no
business holding a clinical site. Idempotent: hospitals that already have a
location are skipped, so re-running never duplicates.
"""

from django.db import migrations

DEFAULT_LOCATION_NAME = "Main Branch"


def create_missing_locations(apps, schema_editor):
    Organization = apps.get_model("organization", "Organization")
    Location = apps.get_model("locations", "Location")

    for org in Organization.objects.filter(status="approved", is_active=True):
        if Location.objects.filter(organization=org, is_active=True).exists():
            continue
        Location.objects.get_or_create(
            organization=org,
            name=DEFAULT_LOCATION_NAME,
            defaults={
                "timezone": org.timezone,
                "phone": org.phone,
                "email": org.email,
                "address_line1": org.address_line1,
                "address_line2": org.address_line2,
                "city": org.city,
                "state": org.state,
                "postal_code": org.postal_code,
                "country": org.country,
            },
        )


def remove_generated_locations(apps, schema_editor):
    """Reverse only the empty defaults this migration could have created.

    A location holding patients is never removed — reversing a schema step must
    not delete clinical records.
    """
    Location = apps.get_model("locations", "Location")
    Location.objects.filter(name=DEFAULT_LOCATION_NAME, patients__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("locations", "0003_location_locations_l_organiz_bd40e7_idx"),
        ("organization", "0005_add_review_status"),
    ]

    operations = [
        migrations.RunPython(create_missing_locations, remove_generated_locations),
    ]
