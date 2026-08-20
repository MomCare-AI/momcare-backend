"""Generic, reusable module activation gate.

One implementation for every program — no per-module copy/paste. Protects the
API layer (``ModuleGatedViewSet``) and below-the-API surfaces such as signal
handlers and tasks (``require_active_module``).
"""

from __future__ import annotations

from functools import wraps

from rest_framework.exceptions import APIException

from momcare_platform.core.organization.models import ModuleRegistry


class ModuleInactive(APIException):
    status_code = 503
    default_detail = "This module is not active."
    default_code = "module_inactive"


class ModuleGatedViewSet:
    """Mixin: returns 503 if ``module_key`` is inactive for the requesting
    user's hospital.

    Unlike Neuro_RPM's single-tenant version of this gate, activation here is
    per-``Organization`` — each hospital independently activates the feature
    modules it's subscribed to (blueprint §2 tenancy decision). Platform
    admins have no single organization, so they bypass this gate entirely —
    module gating is a hospital-subscription concept, not meaningful for a
    cross-hospital operator account.

    Set ``module_key`` on the ViewSet. Must appear before the DRF base class in
    the MRO so its ``initial()`` runs (auth/permissions still run first).
    """

    module_key: str = ""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)  # runs auth/permissions first
        user = request.user
        if user.is_superuser or getattr(user, "organization_id", None) is None:
            return
        if not ModuleRegistry.is_module_active(self.module_key, user.organization_id):
            raise ModuleInactive(detail=f"{self.module_key} module is not active for your hospital.")


def require_active_module(module_key: str, *, organization_id):
    """Decorator for signal handlers / tasks: becomes a no-op if the module is
    inactive for the given hospital. ``organization_id`` must be resolved by
    the caller (e.g. from the instance being processed) since there is no
    request context in a signal/task."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not ModuleRegistry.is_module_active(module_key, organization_id):
                return None
            return fn(*args, **kwargs)

        return wrapper

    return decorator
