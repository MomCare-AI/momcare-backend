"""Row-Level Security for AuditLog.

Reached only through ``user_id`` — AuditLog carries no direct
``organization`` column, the same shape as ``staff_staff`` (see
``0006_row_level_security.py``'s policy for that table, which this mirrors).

``user_id`` is ``on_delete=SET_NULL``, so a row can end up with no user at
all (the account that generated it was later deleted) or one whose
organization has since changed. The EXISTS subquery naturally returns false
for a NULL user_id under every hospital's scope — an audit row that cannot
be attributed to a hospital is not shown to any hospital, only to bypass
(platform admin / Django admin). That is the correct failure direction: a
compliance record either resolves to the caller's own hospital or it is
withheld, never guessed at or shown to the wrong one.
"""

from django.db import migrations

_TABLE = "organization_auditlog"

_USING = """EXISTS (
    SELECT 1 FROM users_user u
    WHERE u.id = organization_auditlog.user_id
      AND u.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
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
        ("organization", "0018_join_request_row_level_security"),
        ("users", "0003_seed_roles"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
