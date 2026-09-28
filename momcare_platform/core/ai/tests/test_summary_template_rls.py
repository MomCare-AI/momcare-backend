"""RLS for ai_aisummarytemplate -- same verification shape as
core/ai/tests/test_instruction_preset_rls.py: confirms the policy exists and
is enabled/forced at the Postgres catalog level. Behavioral enforcement
against a real non-bypassing role is scripts/verify_rls.py's job, not
pytest's -- see that file's own docstring and CLAUDE.md's Tenancy section
for why a pytest session (running as a BYPASSRLS role locally) can never
observe RLS actually blocking anything."""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db


def test_summary_template_table_has_rls_enabled_and_forced():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'ai_aisummarytemplate'",
        )
        row_security, forced = cursor.fetchone()

    assert row_security is True
    assert forced is True


def test_summary_template_table_has_the_tenant_isolation_policy():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT polname FROM pg_policy WHERE polrelid = 'ai_aisummarytemplate'::regclass",
        )
        policy_names = {row[0] for row in cursor.fetchall()}

    assert "tenant_isolation" in policy_names
