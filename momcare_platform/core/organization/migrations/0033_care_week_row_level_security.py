"""Row-Level Security for CareWeek and ReadingAdvice (the weekly care plan tables).

Same fail-closed design and bypass path as 0032 (see 0006's docstring for why NULLIF /
SET LOCAL / FORCE are each necessary). Both hang off a care plan, so each repeats the
join care_plan -> pregnancy -> patient (which carries organization_id directly).
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"
_ORG = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"


def _via_plan(table: str) -> str:
    return f"""EXISTS (
            SELECT 1 FROM care_plans_careplan cp
            JOIN patients_pregnancy pr ON pr.id = cp.pregnancy_id
            JOIN patients_patient p ON p.id = pr.patient_id
            WHERE cp.id = {table}.care_plan_id
              AND p.organization_id = {_ORG}
        )"""


_POLICIES = [
    ("care_plans_careweek", _via_plan("care_plans_careweek")),
    ("care_plans_readingadvice", _via_plan("care_plans_readingadvice")),
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
        ("organization", "0032_care_plan_row_level_security"),
        ("care_plans", "0002_weekly_plans_and_reading_advice"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
