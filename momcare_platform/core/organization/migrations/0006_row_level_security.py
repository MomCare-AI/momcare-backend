"""Postgres Row-Level Security -- the second isolation layer.

Application code already scopes every tenant query through the mixins in
``core/common/scoping.py``. That is one layer: it works only as long as every
query, on every endpoint, remembers to add the filter. This migration adds a
second layer inside the database itself, so a scoping mixin forgotten on one
new endpoint still cannot return another hospital's rows -- the database
refuses, not the application.

FAILS CLOSED BY DESIGN. Every policy compares a column against
``NULLIF(current_setting('app.current_org_id', true), '')``. The ``true``
argument stops an unset variable from raising; the ``NULLIF`` is needed because
a custom Postgres session variable does not reliably revert to SQL NULL once a
transaction that set it via SET LOCAL ends -- in testing it came back as an
empty string, and ``''::uuid`` raises rather than comparing false. Verified by
connecting as a real non-bypassing role: a session with the variable never set
saw zero rows across the board, and a session that had used SET LOCAL earlier
and let the transaction end came back the same way once this fix was in place.
In SQL, ``column = NULL`` evaluates to NULL, never TRUE, so a request that
never set the session variable sees no rows -- not every hospital's rows.
Getting this backwards is the single most dangerous way to build RLS: a policy
that fails open is worse than no policy at all, because it looks like
protection while providing none.

THREE LEGITIMATE PATHS SEE EVERY HOSPITAL, on purpose, and each policy also
allows ``current_setting('app.rls_bypass', true) = 'on'``:

  - ``escalate_alerts`` sweeps every open alert across every hospital every
    five minutes; that is the feature, not a bug to route around.
  - ``seed_demo`` and other management commands run before any request
    exists, so there is no per-request organization to scope to.
  - Django admin, reached by a platform_admin whose own organization_id is
    NULL, is the one place a human is meant to see across tenants.

The bypass flag is set explicitly, in exactly those three call sites, by a
context manager (``core/common/rls.py``) that is never used in the request
path -- so a hospital's own request can never accidentally acquire it.

APPLIES LOCALLY ONLY, TODAY. It has no effect on Neon: the connection there
uses the same superuser role that owns the tables, and that role bypasses RLS
unconditionally regardless of any policy written here -- a limitation of
Postgres itself, not of these policies. Turning this on in production needs a
second, separate change: a dedicated non-superuser application role holding
only the grants it needs, with the connection string switched to it. That is
deliberately not part of this migration -- a credential and connection change
against a live database deserves its own review, tested on its own, not
bundled silently into a schema migration.
"""

from django.db import migrations

# (table, USING clause referencing that table's own columns and, for
# tenant-owned tables reached through a chain, a correlated subquery following
# the exact path documented in CLAUDE.md under "Scoping paths".)
_POLICIES = [
    ("organization_organization", "id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"),
    (
        "users_user",
        "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid",
    ),
    (
        "locations_location",
        "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid",
    ),
    (
        "staff_staff",
        """EXISTS (
            SELECT 1 FROM users_user u
            WHERE u.id = staff_staff.user_id
              AND u.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "monitoring_device",
        "organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid",
    ),
    (
        "patients_patient",
        """EXISTS (
            SELECT 1 FROM locations_location l
            WHERE l.id = patients_patient.location_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "patients_consent",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            JOIN locations_location l ON l.id = p.location_id
            WHERE p.id = patients_consent.patient_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "patients_pregnancy",
        """EXISTS (
            SELECT 1 FROM patients_patient p
            JOIN locations_location l ON l.id = p.location_id
            WHERE p.id = patients_pregnancy.patient_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "patients_pregnancyriskfactors",
        """EXISTS (
            SELECT 1 FROM patients_pregnancy pr
            JOIN patients_patient p ON p.id = pr.patient_id
            JOIN locations_location l ON l.id = p.location_id
            WHERE pr.id = patients_pregnancyriskfactors.pregnancy_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "monitoring_vitalreading",
        """EXISTS (
            SELECT 1 FROM patients_pregnancy pr
            JOIN patients_patient p ON p.id = pr.patient_id
            JOIN locations_location l ON l.id = p.location_id
            WHERE pr.id = monitoring_vitalreading.pregnancy_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "monitoring_riskassessment",
        """EXISTS (
            SELECT 1 FROM patients_pregnancy pr
            JOIN patients_patient p ON p.id = pr.patient_id
            JOIN locations_location l ON l.id = p.location_id
            WHERE pr.id = monitoring_riskassessment.pregnancy_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "alerts_alert",
        """EXISTS (
            SELECT 1 FROM patients_pregnancy pr
            JOIN patients_patient p ON p.id = pr.patient_id
            JOIN locations_location l ON l.id = p.location_id
            WHERE pr.id = alerts_alert.pregnancy_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
    (
        "alerts_alertevent",
        """EXISTS (
            SELECT 1 FROM alerts_alert a
            JOIN patients_pregnancy pr ON pr.id = a.pregnancy_id
            JOIN patients_patient p ON p.id = pr.patient_id
            JOIN locations_location l ON l.id = p.location_id
            WHERE a.id = alerts_alertevent.alert_id
              AND l.organization_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
        )""",
    ),
]

_BYPASS = "current_setting('app.rls_bypass', true) = 'on'"


def _enable_sql() -> str:
    statements = []
    for table, using in _POLICIES:
        condition = f"({using}) OR {_BYPASS}"
        statements.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        # FORCE so the policy also applies to the table's own owner. Necessary
        # in principle, though on a connection that owns the tables AND holds
        # BYPASSRLS -- true of every role on this project's databases today --
        # even FORCE is overridden. See the module docstring.
        statements.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        statements.append(
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL USING ({condition});",
        )
    return "\n".join(statements)


def _disable_sql() -> str:
    statements = []
    for table, _using in _POLICIES:
        statements.append(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        statements.append(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;")
        statements.append(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
    return "\n".join(statements)


class Migration(migrations.Migration):
    dependencies = [
        ("organization", "0005_add_review_status"),
        ("patients", "0003_patient_clinical_identity_and_pregnancy"),
        ("locations", "0004_backfill_main_branch"),
        ("staff", "0003_add_staff_invite"),
        ("monitoring", "0002_risk_assessment"),
        ("alerts", "0001_initial"),
        ("users", "0003_seed_roles"),
    ]

    operations = [
        migrations.RunSQL(sql=_enable_sql(), reverse_sql=_disable_sql()),
    ]
