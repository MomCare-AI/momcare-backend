"""Row-Level Security for ClinicalTag, MonitoringSession and MonitoringNote.

Same fail-closed design and bypass path as 0006; see its docstring for why
NULLIF / SET LOCAL / FORCE are each necessary.

ClinicalTag carries organization_id OR location_id, never both (a model
CheckConstraint enforces it) -- the policy below accepts a direct match on
organization_id (org-level tags) or a join through locations_location for
location_id (location-level tags), so exactly one branch is ever live for
any given row.

MonitoringSession/MonitoringNote are reached through patients_patient,
which carries organization_id directly (denormalized, same as Device) --
no need to go by way of locations_location for these.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "clinical_notes_clinicaltag",
        """
        (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        OR EXISTS (
            SELECT 1 FROM locations_location l
            WHERE l.id = clinical_notes_clinicaltag.location_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )
        """,
    ),
    (
        "clinical_notes_monitoringsession",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            WHERE p.id = clinical_notes_monitoringsession.patient_id
              AND p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "clinical_notes_monitoringnote",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            WHERE p.id = clinical_notes_monitoringnote.patient_id
              AND p.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
]


def _enable_sql() -> str:
    statements = []
    for table, using in _POLICIES:
        condition = f"({using}) OR {_BYPASS}"
        statements.extend(
            [
                f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;",
                f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;",
                f"CREATE POLICY tenant_isolation ON {table} FOR ALL USING ({condition});",
            ],
        )
    return "\n".join(statements)


def _disable_sql() -> str:
    statements = []
    for table, _using in _POLICIES:
        statements.extend(
            [
                f"DROP POLICY IF EXISTS tenant_isolation ON {table};",
                f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;",
                f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;",
            ],
        )
    return "\n".join(statements)


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0019_auditlog_row_level_security"),
        ("clinical_notes", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
