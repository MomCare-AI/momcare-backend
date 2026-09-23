"""Drop ClinicalNote.

Clinical notes are monitoring, not onboarding — the reference platform keeps
them in their own monitoring-notes resource with tags, templates and sessions
around them, which is a feature in its own right rather than a text field on a
pregnancy. Removed here so it can be designed fresh rather than half-existing.

The worklist's "no clinical note in 30 days" reason goes with it: there is no
table left for it to read.

Postgres drops the table's RLS policy along with the table.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0012_flatten_risk_factors_and_consent"),
        ("organization", "0016_program_enrollment_row_level_security"),
    ]

    operations = [
        migrations.DeleteModel(name="ClinicalNote"),
    ]
