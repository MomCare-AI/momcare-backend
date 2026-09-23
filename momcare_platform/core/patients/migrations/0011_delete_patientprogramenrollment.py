"""Drop PatientProgramEnrollment.

It duplicated a role ``Pregnancy`` already fills. MomCare runs one programme —
maternal monitoring — so a patient's enrol→discharge episode *is* her pregnancy:
same start date, same status, same one-open-at-a-time rule. Carrying a second
episode table meant two concepts for one fact and an API surface with no
decision behind it (every enrollment was always ``"rpm"``).

If a second programme ever arrives, this comes back then, on evidence.

The table's RLS policy (organization.0016) is dropped by Postgres along with
the table itself, so no separate policy migration is needed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0010_patientprogramenrollment"),
        ("organization", "0016_program_enrollment_row_level_security"),
    ]

    operations = [
        migrations.DeleteModel(name="PatientProgramEnrollment"),
    ]
