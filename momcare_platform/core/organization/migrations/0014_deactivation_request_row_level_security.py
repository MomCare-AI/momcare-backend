"""Row-Level Security for OrganizationDeactivationRequest -- the same second
layer every other tenant-owned table already has (see
0006_row_level_security's own docstring for the full fail-closed reasoning:
NULLIF/SET LOCAL/FORCE, and why each is necessary). Not repeated here.

Simpler than most of 0006's policies: organization_id is a direct column on
this table (same shape as users_user/locations_location/monitoring_device),
not reached through a Location chain.
"""

from django.db import migrations

_TABLE = "organization_organizationdeactivationrequest"

_USING = "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

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
        ("organization", "0013_organizationdeactivationrequest"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
