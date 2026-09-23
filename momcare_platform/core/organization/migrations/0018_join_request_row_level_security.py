"""Row-Level Security for PatientJoinRequest.

Scoped on the table's own ``organization_id``, so a hospital only ever sees
requests addressed to itself.

The patient side is deliberately NOT expressible here. She has no
organization, so her token carries no ``org_id`` claim and the fail-closed
policy shows her nothing — which is correct for every tenant-owned table but
would also hide her own request from her. Her two endpoints therefore run
inside ``bypass_rls()`` with an explicit ``user=request.user`` filter, the
same sanctioned pattern ``escalate_alerts`` and Django admin already use. The
filter, not the policy, is what keeps her to her own rows there.
"""

from django.db import migrations

_TABLE = "patients_patientjoinrequest"

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
        ("organization", "0017_secondary_provider_row_level_security"),
        ("patients", "0015_patient_join_request"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
