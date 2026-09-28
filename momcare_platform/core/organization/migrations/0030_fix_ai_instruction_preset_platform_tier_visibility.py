"""Fix ai_aiinstructionpreset's RLS policy: the platform tier must be
readable by every hospital session, not just by bypass_rls() callers.

0028's policy was ``organization_id = NULLIF(current_org_id, '')::uuid``.
A platform-tier row has organization_id IS NULL, and NULL never equals a
uuid via ``=`` -- so an ordinary hospital-scoped session (the read path
every request-driven AI Summary trigger actually uses; only the periodic
refresh_ai_summaries command calls bypass_rls()) could never see a
platform-tier preset at all. Found by code review, reproduced against a
real non-bypassing role via scripts/verify_rls.py before this fix, and
confirmed passing after it.

Splits USING from WITH CHECK rather than widening one shared condition:
- USING (read path): a hospital's own rows, OR any platform-tier row
  (organization_id IS NULL) -- platform instructions are meant for every
  hospital, the same as AIProviderConfig itself (which carries no RLS
  at all, being genuinely platform-wide data).
- WITH CHECK (write path): a hospital's own rows ONLY -- deliberately
  narrower than USING, so an ordinary hospital session still cannot
  create or repoint a platform-tier preset. Without an explicit WITH
  CHECK, Postgres reuses the USING expression for both, which would
  have let any hospital session write organization_id = NULL rows.

Does not touch 0028 in place -- it's already applied (locally, and
would be in production once deployed), and migrations that have already
run are never edited after the fact in this codebase.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"
_OWN_ORG = "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

_USING = f"({_OWN_ORG}) OR organization_id IS NULL OR {_BYPASS}"
_WITH_CHECK = f"({_OWN_ORG}) OR {_BYPASS}"

_ENABLE_SQL = f"""
DROP POLICY IF EXISTS tenant_isolation ON ai_aiinstructionpreset;
CREATE POLICY tenant_isolation ON ai_aiinstructionpreset FOR ALL
    USING ({_USING})
    WITH CHECK ({_WITH_CHECK});
"""

_DISABLE_SQL = f"""
DROP POLICY IF EXISTS tenant_isolation ON ai_aiinstructionpreset;
CREATE POLICY tenant_isolation ON ai_aiinstructionpreset FOR ALL
    USING (({_OWN_ORG}) OR {_BYPASS});
"""


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0029_remove_organization_ai_custom_instructions"),
    ]

    operations = [
        migrations.RunSQL(sql=_ENABLE_SQL, reverse_sql=_DISABLE_SQL),
    ]
