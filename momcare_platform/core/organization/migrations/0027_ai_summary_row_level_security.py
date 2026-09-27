"""Row-Level Security for AISummary.

Same fail-closed design and bypass path as 0006/0020/0023/0024/0025; see
0006's docstring for why NULLIF / SET LOCAL / FORCE are each necessary.

AISummary is reached through patients_patient, which carries organization_id
directly (denormalized, same as Device) -- same shape as 0025's
PatientAnalytics policy.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "ai_aisummary",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            WHERE p.id = ai_aisummary.patient_id
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
        ("organization", "0026_organization_ai_custom_instructions"),
        ("ai", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
