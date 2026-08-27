"""Prove the row-level security policies work, against a role that cannot
bypass them.

Not a pytest test, on purpose. Proving RLS needs a real, ordinary Postgres
role -- ``CREATE ROLE`` and the data it must see have to actually commit, so a
second physical connection can see them, and that requires disabling the
rollback-based isolation pytest normally gives each test. Trying that inside
the shared test suite corrupted the migration-seeded Role rows for every other
test that ran after it, in whatever process ran next -- discovered the hard
way while building this. That risk is not worth taking against a database
other tests depend on, so this runs on demand instead, against a disposable
scratch database, and cleans up after itself even on failure.

Run it after touching the policies in
``organization/migrations/0006_row_level_security.py``:

    uv run python scripts/verify_rls.py

Exit code is 0 only if every check below passed.
"""

from __future__ import annotations

import os
import sys
import uuid

import django
import psycopg

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()

from django.conf import settings  # noqa: E402
from django.db import connections  # noqa: E402

SCRATCH_DB = f"rls_verify_{uuid.uuid4().hex[:8]}"
ROLE = "rls_verify_probe"
PASSWORD = "verify-only-not-a-real-credential"

failures: list[str] = []


def check(label: str, condition: bool) -> None:
    mark = "OK  " if condition else "FAIL"
    print(f"  [{mark}] {label}")
    if not condition:
        failures.append(label)


def admin_conn():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        host=db["HOST"] or "localhost",
        port=db["PORT"] or 5432,
        dbname="postgres",
        user=db["USER"],
        password=db["PASSWORD"],
        autocommit=True,
    )


def probe_conn(dbname: str):
    db = settings.DATABASES["default"]
    conn = psycopg.connect(
        host=db["HOST"] or "localhost",
        port=db["PORT"] or 5432,
        dbname=dbname,
        user=ROLE,
        password=PASSWORD,
    )
    conn.autocommit = True
    return conn


def main() -> int:
    print(f"Cloning the current schema into a scratch database: {SCRATCH_DB}")
    src_db = settings.DATABASES["default"]["NAME"]

    with admin_conn() as admin:
        with admin.cursor() as cur:
            # A role left behind by an interrupted previous run would make the
            # DROP ROLE below fail with "objects still depend on it" - so any
            # stale scratch database sharing this script's naming pattern goes
            # first, then the role, in the only order Postgres accepts.
            cur.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'rls_verify_%'",
            )
            for (stale_db,) in cur.fetchall():
                cur.execute(f'DROP DATABASE IF EXISTS "{stale_db}" WITH (FORCE)')
            cur.execute(f'DROP ROLE IF EXISTS "{ROLE}"')

            cur.execute(f'CREATE DATABASE "{SCRATCH_DB}" TEMPLATE "{src_db}"')
            cur.execute(f"CREATE ROLE \"{ROLE}\" LOGIN PASSWORD '{PASSWORD}' NOBYPASSRLS")

        try:
            with psycopg.connect(
                host=admin.info.host,
                port=admin.info.port,
                dbname=SCRATCH_DB,
                user=admin.info.user,
                password=settings.DATABASES["default"]["PASSWORD"],
                autocommit=True,
            ) as scratch_admin:
                with scratch_admin.cursor() as cur:
                    cur.execute(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{ROLE}"')

                # Two tenants, one patient each - the minimum needed to tell
                # isolation from coincidence. Built through Django's own ORM,
                # pointed at the scratch database, so every required column
                # gets its real default rather than being hand-guessed here.
                connections["default"].settings_dict["NAME"] = SCRATCH_DB
                connections["default"].close()

                from momcare_platform.core.locations.models import Location  # noqa: PLC0415
                from momcare_platform.core.organization.models import Organization  # noqa: PLC0415
                from momcare_platform.core.patients.models import Patient  # noqa: PLC0415

                def make(name: str):
                    org = Organization.objects.create(name=name, country="Pakistan")
                    location = Location.objects.create(organization=org, name="Main")
                    Patient.objects.create(location=location, first_name="Test", last_name=name)
                    return org.id

                ours = make("Verify Hospital A")
                make("Verify Hospital B")
                connections["default"].close()

                # The scratch database is a full TEMPLATE clone, so it carries
                # whatever real data already exists locally - the bypass check
                # below has to compare against that true total, not against
                # the two rows this script itself just added.
                with scratch_admin.cursor() as cur:
                    cur.execute("SELECT count(*) FROM patients_patient")
                    total_patients = cur.fetchone()[0]

                print("\nChecking policies as a role that cannot bypass RLS:")

                with probe_conn(SCRATCH_DB) as conn, conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM patients_patient")
                    check("never scoped -> sees zero rows (fail closed)", cur.fetchone()[0] == 0)

                with probe_conn(SCRATCH_DB) as conn, conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute(
                        "SELECT set_config('app.current_org_id', %s, true)",
                        [str(ours)],
                    )
                    cur.execute("SELECT count(*) FROM patients_patient")
                    count = cur.fetchone()[0]
                    cur.execute("COMMIT")
                    check("scoped to hospital A -> sees exactly 1 row", count == 1)

                    # The bug this whole script exists to catch: a custom
                    # session variable can come back as '' rather than NULL
                    # once its SET LOCAL transaction ends.
                    cur.execute("BEGIN")
                    cur.execute("SELECT count(*) FROM patients_patient")
                    leaked = cur.fetchone()[0]
                    cur.execute("COMMIT")
                    check("variable does not leak into the next transaction", leaked == 0)

                with probe_conn(SCRATCH_DB) as conn, conn.cursor() as cur:
                    cur.execute("BEGIN")
                    cur.execute("SELECT set_config('app.rls_bypass', 'on', true)")
                    cur.execute("SELECT count(*) FROM patients_patient")
                    count = cur.fetchone()[0]
                    cur.execute("COMMIT")
                    check(
                        f"bypass flag -> sees every hospital's rows ({total_patients} total)",
                        count == total_patients,
                    )

                with probe_conn(SCRATCH_DB) as conn, conn.cursor() as cur:
                    cur.execute("BEGIN")
                    raised = False
                    try:
                        cur.execute("SET LOCAL app.current_org_id = 'not-a-uuid'")
                        cur.execute("SELECT count(*) FROM patients_patient")
                    except psycopg.errors.InvalidTextRepresentation:
                        raised = True
                    conn.rollback()
                    check("a malformed org id errors, not silently ignored", raised)

        finally:
            connections["default"].settings_dict["NAME"] = src_db
            connections["default"].close()
            # The database must go first: the role's grants live inside it,
            # and Postgres refuses to drop a role anything still depends on.
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}" WITH (FORCE)')
                cur.execute(f'DROP ROLE IF EXISTS "{ROLE}"')

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed. RLS policies are fail-closed and correctly scoped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
