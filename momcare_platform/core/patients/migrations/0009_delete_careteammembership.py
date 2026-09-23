"""Drop CareTeamMembership — superseded by Pregnancy's three direct
provider/nurse/care_manager columns (0008).

The table's Row-Level Security policy (organization.0007_care_team_row_level_
security) goes with it automatically: Postgres drops a table's policies when
the table itself is dropped, so no separate policy-removal migration is needed.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0008_pregnancy_provider_nurse_care_manager"),
        ("organization", "0007_care_team_row_level_security"),
    ]

    operations = [
        migrations.DeleteModel(name="CareTeamMembership"),
    ]
