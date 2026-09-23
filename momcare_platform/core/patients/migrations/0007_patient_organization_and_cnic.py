"""Patient gains a direct organization FK, a per-hospital CNIC uniqueness
constraint, and an emergency contact email.

The organization column is denormalized from location.organization (the same
shape Device already uses) specifically so the CNIC constraint can be scoped
to the hospital: a hospital can have several locations, so a location-scoped
constraint would miss a duplicate CNIC at a different branch.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_organization(apps, schema_editor):
    Patient = apps.get_model("patients", "Patient")
    for patient in Patient.objects.select_related("location").iterator():
        patient.organization_id = patient.location.organization_id
        patient.save(update_fields=["organization"])


def normalize_empty_cnic_to_null(apps, schema_editor):
    Patient = apps.get_model("patients", "Patient")
    Patient.objects.filter(cnic="").update(cnic=None)


def check_no_duplicate_cnics(apps, schema_editor):
    """Fail with a readable message rather than a raw IntegrityError out of
    AddConstraint, if any hospital already has two patients sharing a CNIC."""
    from django.db.models import Count

    Patient = apps.get_model("patients", "Patient")
    duplicates = list(
        Patient.objects.exclude(cnic__isnull=True)
        .values("organization_id", "cnic")
        .annotate(n=Count("id"))
        .filter(n__gt=1),
    )
    if duplicates:
        raise RuntimeError(
            "Cannot add unique_cnic_per_organization: found existing duplicate CNICs "
            f"within a hospital: {duplicates}. Resolve these records before migrating.",
        )


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0015_add_license_number_and_image"),
        ("patients", "0006_careteammembership_ended_by"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="organization",
            field=models.ForeignKey(
                to="organization.organization",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="patients",
                null=True,  # temporary — backfilled below, then locked down
            ),
        ),
        migrations.RunPython(backfill_organization, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="patient",
            name="organization",
            field=models.ForeignKey(
                to="organization.organization",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="patients",
            ),
        ),
        migrations.AlterField(
            model_name="patient",
            name="cnic",
            field=models.CharField(max_length=20, blank=True, null=True, db_index=True, verbose_name="CNIC"),
        ),
        migrations.RunPython(normalize_empty_cnic_to_null, migrations.RunPython.noop),
        migrations.RunPython(check_no_duplicate_cnics, migrations.RunPython.noop),
        # The backfill above queues deferred FK trigger events, and Postgres
        # refuses to CREATE INDEX (which AddConstraint does) while any are
        # still pending. Forcing them to fire now clears the queue.
        migrations.RunSQL(
            sql="SET CONSTRAINTS ALL IMMEDIATE;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.AddConstraint(
            model_name="patient",
            constraint=models.UniqueConstraint(
                fields=("organization", "cnic"),
                condition=models.Q(("cnic__isnull", False)),
                name="unique_cnic_per_organization",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="emergency_contact_email",
            field=models.EmailField(max_length=254, blank=True, default=""),
        ),
    ]
