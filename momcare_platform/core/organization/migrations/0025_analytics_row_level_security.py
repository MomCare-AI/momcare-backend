"""Row-Level Security for PatientAnalytics.

Same fail-closed design and bypass path as 0006/0020/0023/0024; see 0006's
docstring for why NULLIF / SET LOCAL / FORCE are each necessary.

PatientAnalytics is reached through patients_patient, which carries
organization_id directly (denormalized, same as Device) -- same shape as
0020's MonitoringSession/MonitoringNote policies and 0023's PatientStatus.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "analytics_patientanalytics",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            WHERE p.id = analytics_patientanalytics.patient_id
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
        ("organization", "0024_note_template_row_level_security"),
        ("analytics", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
