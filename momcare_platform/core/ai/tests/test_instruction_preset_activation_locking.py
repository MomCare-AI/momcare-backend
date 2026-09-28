"""activate_instruction_preset()'s scope lock -- transaction.atomic() alone
gives atomicity, not serializability, so two concurrent activations of two
DIFFERENT (not-yet-active) presets in the same scope could otherwise both
commit under READ COMMITTED, leaving two active rows (neither activation's
"deactivate the others" UPDATE touches the other's row, since neither is
active yet -- there is no shared row for them to contend over without an
explicit lock).

Deliberately does NOT use pytest.mark.django_db(transaction=True) to prove
this end-to-end with real concurrent threads: that flushes the database
between tests and does not restore migration-seeded data (Role rows) for
whatever test runs next in this shared test database -- the exact incident
scripts/verify_rls.py's own docstring documents hitting while building that
script. These two tests instead verify the locking primitive itself, safely,
within the normal transactional test wrapping.
"""

import psycopg
import pytest
from django.conf import settings
from django.db import connection, transaction

from momcare_platform.core.ai.services import _lock_instruction_preset_scope

pytestmark = pytest.mark.django_db


def test_lock_instruction_preset_scope_acquires_a_real_advisory_lock():
    with transaction.atomic():
        _lock_instruction_preset_scope("11111111-1111-1111-1111-111111111111")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND pid = pg_backend_pid()",
            )
            (count,) = cursor.fetchone()

    assert count == 1


def test_pg_advisory_xact_lock_blocks_a_second_holder_until_the_first_commits():
    """Proves the underlying Postgres primitive _lock_instruction_preset_scope
    is built on actually serializes two concurrent holders of the same key --
    independent of Django's ORM/transaction wrapping, via two raw connections
    to the same (already-migrated) test database. Advisory locks are
    session-level, not part of MVCC row visibility, so this needs no access
    to any Django-created row data and is safe under the default, transaction-
    wrapped django_db fixture."""
    info = connection.connection.info
    password = settings.DATABASES["default"]["PASSWORD"]
    conn1 = psycopg.connect(host=info.host, port=info.port, dbname=info.dbname, user=info.user, password=password)
    conn2 = psycopg.connect(host=info.host, port=info.port, dbname=info.dbname, user=info.user, password=password)
    try:
        with conn1.cursor() as cur1, conn2.cursor() as cur2:
            cur1.execute("BEGIN")
            cur1.execute("SELECT pg_advisory_xact_lock(hashtext('test-scope-key'))")

            cur2.execute("BEGIN")
            cur2.execute("SELECT pg_try_advisory_xact_lock(hashtext('test-scope-key'))")
            row = cur2.fetchone()
            assert row is not None
            (acquired_while_held,) = row
            cur2.execute("COMMIT")

            cur1.execute("COMMIT")

            cur2.execute("BEGIN")
            cur2.execute("SELECT pg_try_advisory_xact_lock(hashtext('test-scope-key'))")
            row = cur2.fetchone()
            assert row is not None
            (acquired_after_release,) = row
            cur2.execute("COMMIT")
    finally:
        conn1.close()
        conn2.close()

    assert acquired_while_held is False
    assert acquired_after_release is True
