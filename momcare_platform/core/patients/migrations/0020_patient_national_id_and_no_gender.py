"""Rename ``cnic`` to ``national_id`` and drop ``gender`` on Patient.

A rename, not a drop-and-add: existing national IDs must survive.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0019_patient_dietary_preference_patient_food_allergies"),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="patient", name="unique_cnic_per_organization"),
        migrations.RemoveIndex(model_name="patient", name="patients_pa_cnic_6072c8_idx"),
        migrations.RenameField(model_name="patient", old_name="cnic", new_name="national_id"),
        migrations.AddConstraint(
            model_name="patient",
            constraint=models.UniqueConstraint(
                condition=models.Q(("national_id__isnull", False)),
                fields=("organization", "national_id"),
                name="unique_national_id_per_organization",
            ),
        ),
        migrations.AddIndex(
            model_name="patient",
            index=models.Index(fields=["national_id"], name="patients_pa_nationa_70cc77_idx"),
        ),
        migrations.AlterField(
            model_name="patient",
            name="national_id",
            field=models.CharField(blank=True, db_index=True, max_length=20, null=True, verbose_name="national ID"),
        ),
        migrations.RemoveField(model_name="patient", name="gender"),
    ]
