"""Row-Level Security for CareTeamMembership -- the same second layer every
other tenant-owned table already has, extended to the one table added since
0006_row_level_security.

Same fail-closed design, same reasoning, same bypass paths -- see 0006's own
docstring for the full explanation of why NULLIF/SET LOCAL/FORCE are each
necessary. Not repeated here; this migration only adds the one new table to
the existing scheme, using the exact same USING clause pattern as
patients_pregnancy (this table sits at the same point in the ownership chain
-- a pregnancy's own care team -- so it is scoped identically).
"""

from django.db import migrations

_TABLE = "patients_careteammembership"

_USING = """EXISTS (
    SELECT 1 FROM patients_pregnancy pr
    JOIN patients_patient p ON p.id = pr.patient_id
    JOIN locations_location l ON l.id = p.location_id
    WHERE pr.id = patients_careteammembership.pregnancy_id
      AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
)"""

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"


def _enable_sql() -> str:
    condition = f"({_USING}) OR {_BYPASS}"
    return "\n".join(
        [
            f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY;",
            f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY;",
            f"CREATE POLICY tenant_isolation ON {_TABLE} FOR ALL USING ({condition});",
        ],
    )


def _disable_sql() -> str:
    return "\n".join(
        [
            f"DROP POLICY IF EXISTS tenant_isolation ON {_TABLE};",
            f"ALTER TABLE {_TABLE} NO FORCE ROW LEVEL SECURITY;",
            f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY;",
        ],
    )


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0006_row_level_security"),
        ("patients", "0005_careteammembership"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
