"""MomCare's maternal-risk model — the AI seam referenced in CLAUDE.md.

A plain Python package, not a Django app: no models, no migrations, no
framework dependency. It sits at the repo root beside ``config/`` and
``momcare_platform/`` rather than nested inside either, because it is neither
project wiring nor a Django app — the same reasoning that keeps ``config/``
separate applies here.

``core.monitoring.services`` is the only Django code that imports from this
package, and it does so with a function-local import (see reassess_risk()),
the same pattern already used for the alerts/monitoring cross-import.
"""
