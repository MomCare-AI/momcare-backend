"""Row-Level Security for the Monthly Care Plan tables.

Same fail-closed design and bypass path as 0006/0020/0027; see 0006's docstring
for why NULLIF / SET LOCAL / FORCE are each necessary.

``CarePlan`` is reached through its pregnancy's patient, which carries
``organization_id`` directly (same shape as 0020's MonitoringNote policy).
Everything that hangs off a plan (versions, adjustments, medications, notes)
repeats that join through ``care_plan`` -> ``pregnancy`` -> ``patient`` rather
than relying on the plan table's own policy being applied inside theirs.
``PlanCorrection`` and ``HospitalPreference`` carry ``organization_id`` themselves.
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
    (
        "care_plans_careplan",
        f"""EXISTS (
            SELECT 1 FROM patients_pregnancy pr
            JOIN patients_patient p ON p.id = pr.patient_id
            WHERE pr.id = care_plans_careplan.pregnancy_id
              AND p.organization_id = {_ORG}
        )""",
    ),
    ("care_plans_careplansectionversion", _via_plan("care_plans_careplansectionversion")),
    ("care_plans_careplanadjustment", _via_plan("care_plans_careplanadjustment")),
    ("care_plans_careplanmedication", _via_plan("care_plans_careplanmedication")),
    ("care_plans_careplannote", _via_plan("care_plans_careplannote")),
    ("care_plans_plancorrection", f"organization_id = {_ORG}"),
    ("care_plans_hospitalpreference", f"organization_id = {_ORG}"),
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
        ("organization", "0031_ai_summary_template_row_level_security"),
        ("care_plans", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
