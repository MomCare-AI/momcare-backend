"""The care team becomes three direct columns on Pregnancy.

``assigned_staff`` is renamed to ``provider`` (same column, no data movement)
and joined by ``nurse`` and ``care_manager``. This is what replaces the
CareTeamMembership join table, deleted in the next migration — one of each
role at a time, no rotation, no handoff history.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0007_patient_organization_and_cnic"),
        ("staff", "0006_remove_staff_invite"),
    ]

    operations = [
        migrations.RenameField(
            model_name="pregnancy",
            old_name="assigned_staff",
            new_name="provider",
        ),
        migrations.AddField(
            model_name="pregnancy",
            name="nurse",
            field=models.ForeignKey(
                to="staff.staff",
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                blank=True,
                related_name="nursed_pregnancies",
            ),
        ),
        migrations.AddField(
            model_name="pregnancy",
            name="care_manager",
            field=models.ForeignKey(
                to="staff.staff",
                on_delete=django.db.models.deletion.PROTECT,
                null=True,
                blank=True,
                related_name="care_managed_pregnancies",
            ),
        ),
    ]
