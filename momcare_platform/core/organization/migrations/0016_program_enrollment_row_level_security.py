"""Row-Level Security for PatientProgramEnrollment -- the same second layer
every other tenant-owned table already has.

Same fail-closed design, same bypass paths as 0006 -- see 0006's own docstring
for the full explanation of why NULLIF/SET LOCAL/FORCE are each necessary. This
migration only adds the one new table to the existing scheme, scoped through
its patient's location the same way patients_pregnancy is.
"""

from django.db import migrations

_TABLE = "patients_patientprogramenrollment"

_USING = """EXISTS (
    SELECT 1 FROM patients_patient p
    JOIN locations_location l ON l.id = p.location_id
    WHERE p.id = patients_patientprogramenrollment.patient_id
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
        ("organization", "0015_add_license_number_and_image"),
        ("patients", "0010_patientprogramenrollment"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
