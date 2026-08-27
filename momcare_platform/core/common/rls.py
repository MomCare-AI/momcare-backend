"""Telling Postgres which hospital is asking.

The RLS policies in ``organization.0006_row_level_security`` read two session
variables: ``app.current_org_id`` and ``app.rls_bypass``. This module is the
only place either is ever set. Nothing else in the codebase should call
``SET`` on them directly -- a second call site is a second place the fail-closed
guarantee could be gotten wrong.

Both use ``SET LOCAL``, which only ever applies inside an open transaction and
is discarded when it ends. That matters more than it looks: Django reuses
pooled database connections across requests (``CONN_MAX_AGE``), so a plain
``SET`` would leak one hospital's session variable into whichever request
happens to reuse that connection next -- a cross-tenant leak caused by the very
mechanism meant to prevent one. ``SET LOCAL`` cannot do that, because it dies
with the transaction it was set inside.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction


def set_current_organization(organization_id) -> None:
    """Scope every query on this connection, for the rest of the open transaction.

    Call inside an already-open transaction (the request middleware wraps the
    whole request in one). Outside a transaction ``SET LOCAL`` raises, which is
    the correct failure -- it means the caller has not thought about when this
    stops applying.
    """
    with connection.cursor() as cursor:
        # SET LOCAL is a statement, not a function call, and Postgres does not
        # accept bind parameters in it - %s here would be a syntax error, not
        # a substitution. set_config() is the parameterised equivalent; its
        # third argument (true) is what makes it transaction-local rather than
        # session-wide, matching SET LOCAL's own scope.
        cursor.execute(
            "SELECT set_config('app.current_org_id', %s, true)",
            [str(organization_id)],
        )


@contextmanager
def bypass_rls() -> Iterator[None]:
    """Cross-tenant access for the three paths that legitimately need it:
    ``escalate_alerts``, ``seed_demo`` and other management commands, and
    Django admin for a platform administrator.

    Never call this from request-handling code. Everything reached from an
    ordinary view runs under the organization set by the request middleware,
    and that is what keeps one hospital's request from ever seeing another's
    data by accident.
    """
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL app.rls_bypass = 'on'")
        yield
