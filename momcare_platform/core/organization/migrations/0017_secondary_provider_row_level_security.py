"""Row-Level Security for SecondaryProvider.

A hospital's referral list is tenant-owned data — without this policy a
non-bypassing role could read every other hospital's external clinicians.
Same fail-closed design and bypass path as 0006; see its docstring for why
NULLIF / SET LOCAL / FORCE are each necessary.

Scoped on the table's own ``organization_id`` (like Device), not through a
join, because SecondaryProvider carries the column directly.
"""

from django.db import migrations

_TABLE = "staff_secondaryprovider"

_USING = (
    "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"
)

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
        ("organization", "0016_program_enrollment_row_level_security"),
        ("staff", "0007_secondary_provider"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
