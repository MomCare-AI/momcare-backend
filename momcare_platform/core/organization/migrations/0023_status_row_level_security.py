"""Row-Level Security for StatusLabel and PatientStatus.

Same fail-closed design and bypass path as 0006/0020; see 0006's docstring
for why NULLIF / SET LOCAL / FORCE are each necessary.

StatusLabel carries organization_id OR location_id, never both (a model
CheckConstraint enforces it) -- same shape as 0020's ClinicalTag policy.

PatientStatus is reached through patients_patient, which carries
organization_id directly (denormalized, same as Device) -- same shape as
0020's MonitoringSession/MonitoringNote policies.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "clinical_notes_statuslabel",
        """
        (organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid)
        OR EXISTS (
            SELECT 1 FROM locations_location l
            WHERE l.id = clinical_notes_statuslabel.location_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )
        """,
    ),
    (
        "clinical_notes_patientstatus",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            WHERE p.id = clinical_notes_patientstatus.patient_id
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
        ("organization", "0022_notification_row_level_security"),
        ("clinical_notes", "0002_statuslabel_patientstatus"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
