"""RLS on every care plan table -- the database half of the tenancy defence.

Checks that each table has row-level security enabled *and forced* and carries
the tenant_isolation policy. The behavioural proof (a non-bypassing role really
sees only its own hospital's rows, and zero when unscoped) lives in
scripts/verify_rls.py, which needs a second physical connection and so cannot
run inside pytest's rolled-back transactions.
"""

import pytest
from django.db import connection

pytestmark = pytest.mark.django_db

TABLES = [
    "care_plans_careplan",
    "care_plans_careplansectionversion",
    "care_plans_careplanadjustment",
    "care_plans_careplanmedication",
    "care_plans_careplannote",
    "care_plans_careweek",
    "care_plans_readingadvice",
    "care_plans_plancorrection",
    "care_plans_hospitalpreference",
]


@pytest.mark.parametrize("table", TABLES)
def test_rls_is_enabled_and_forced(table):
    with connection.cursor() as cursor:
        cursor.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s", [table])
        row_security, forced = cursor.fetchone()
    assert row_security is True
    assert forced is True


@pytest.mark.parametrize("table", TABLES)
def test_the_tenant_isolation_policy_exists(table):
    with connection.cursor() as cursor:
        cursor.execute("SELECT polname FROM pg_policy WHERE polrelid = %s::regclass", [table])
        assert "tenant_isolation" in {row[0] for row in cursor.fetchall()}
