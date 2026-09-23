"""PatientProgramEnrollment — one row per enrol→discharge episode, per program.

Only one program exists today ("rpm", shown as "Maternal Monitoring"). The
table earns its place by making a second program a new choice rather than a
schema redesign.

Existing patients are backfilled with an open enrollment dated to when they
were created, so no pre-existing record is left without one.
"""

import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_rpm_enrollment_for_existing_patients(apps, schema_editor):
    Patient = apps.get_model("patients", "Patient")
    PatientProgramEnrollment = apps.get_model("patients", "PatientProgramEnrollment")
    PatientProgramEnrollment.objects.bulk_create(
        [
            PatientProgramEnrollment(
                patient=patient,
                program_code="rpm",
                status="enrolled",
                enrolled_at=patient.created_at.date(),
            )
            for patient in Patient.objects.all().iterator()
        ],
    )


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0009_delete_careteammembership"),
    ]

    operations = [
        migrations.CreateModel(
            name="PatientProgramEnrollment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "program_code",
                    models.CharField(choices=[("rpm", "Maternal Monitoring")], db_index=True, max_length=20),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("enrolled", "Enrolled"),
                            ("paused", "Paused"),
                            ("discharged", "Discharged"),
                        ],
                        default="enrolled",
                        max_length=20,
                    ),
                ),
                ("enrolled_at", models.DateField()),
                ("disenrolled_at", models.DateField(blank=True, null=True)),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="program_enrollments",
                        to="patients.patient",
                    ),
                ),
            ],
            options={"ordering": ["-enrolled_at"]},
        ),
        migrations.AddConstraint(
            model_name="patientprogramenrollment",
            constraint=models.UniqueConstraint(
                condition=models.Q(("disenrolled_at__isnull", True)),
                fields=("patient", "program_code"),
                name="one_open_enrollment_per_program",
            ),
        ),
        migrations.RunPython(backfill_rpm_enrollment_for_existing_patients, migrations.RunPython.noop),
    ]
