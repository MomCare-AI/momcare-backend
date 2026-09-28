"""Row-Level Security for AIInstructionPreset.

Same fail-closed design and bypass path as 0006/0020/0023/0024/0025/0027;
see 0006's docstring for why NULLIF / SET LOCAL / FORCE are each necessary.

organization is a direct column, same shape as 0006's monitoring_device
policy -- and since NULL never equals a real uuid via `=`, a platform-tier
row (organization_id IS NULL) is invisible to every hospital session without
any special-case clause: the USING condition simply never matches it.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"

_POLICIES = [
    (
        "ai_aiinstructionpreset",
        "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid",
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
        ("organization", "0027_ai_summary_row_level_security"),
        ("ai", "0003_aiinstructionpreset"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
