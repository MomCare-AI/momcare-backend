"""Row-Level Security for ai_aisummarytemplate.

Written directly with the split USING/WITH CHECK shape
0030_fix_ai_instruction_preset_platform_tier_visibility.py arrived at after
review -- this table is new, so there is no need to reproduce 0028's
original (wrong) single-condition mistake as a first migration and then a
separate fix; it goes in correctly from the start.

Same reasoning as that fix: a platform-tier template (organization IS
NULL) must be readable by every hospital session, not just bypass_rls()
callers -- _resolve_active_template()'s own read runs under an ordinary
org-scoped session for every request-driven AI Summary trigger, exactly
the same read path AIInstructionPreset's platform tier needed fixed for.
Only the periodic refresh_ai_summaries command calls bypass_rls().

- USING (read path): a hospital's own rows, OR any platform-tier row
  (organization_id IS NULL).
- WITH CHECK (write path): a hospital's own rows ONLY -- so an ordinary
  hospital session still cannot create or repoint a platform-tier
  template. Without an explicit WITH CHECK, Postgres reuses the USING
  expression for both, which would let any hospital session write
  organization_id = NULL rows.
"""

from django.db import migrations

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"
_OWN_ORG = "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"

_USING = f"({_OWN_ORG}) OR organization_id IS NULL OR {_BYPASS}"
_WITH_CHECK = f"({_OWN_ORG}) OR {_BYPASS}"

_ENABLE_SQL = f"""
ALTER TABLE ai_aisummarytemplate ENABLE ROW LEVEL SECURITY;
ALTER TABLE ai_aisummarytemplate FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON ai_aisummarytemplate FOR ALL
    USING ({_USING})
    WITH CHECK ({_WITH_CHECK});
"""

_DISABLE_SQL = """
DROP POLICY IF EXISTS tenant_isolation ON ai_aisummarytemplate;
ALTER TABLE ai_aisummarytemplate NO FORCE ROW LEVEL SECURITY;
ALTER TABLE ai_aisummarytemplate DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0030_fix_ai_instruction_preset_platform_tier_visibility"),
        ("ai", "0006_aisummarytemplate"),
    ]

    operations = [
        migrations.RunSQL(sql=_ENABLE_SQL, reverse_sql=_DISABLE_SQL),
    ]
