"""AuditLogMiddleware — HIPAA-essential PHI access logging.

Records one ``AuditLog`` row per request to a PHI-touching endpoint
(``/api/patients``, ``/api/alerts``, ``/api/attention`` — extend
``_PHI_PREFIXES`` as feature modules add their own PHI-bearing endpoints). Writes are synchronous (a single cheap
insert) and best-effort — logging must never break a request.

Note: user attribution relies on ``request.user``. Session-authenticated
requests (admin, Swagger) are captured accurately; refining token-auth
attribution can be layered on later without changing this contract.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.urls import Resolver404, resolve
from django.utils.cache import add_never_cache_headers

from momcare_platform.core.common.request_logging import request_id_ctx, user_id_ctx

logger = logging.getLogger(__name__)

# Path prefixes considered PHI-touching.
# Alerts and the attention queue carry patient names and clinical findings, so
# reading them is access to PHI exactly as reading the record is.
_PHI_PREFIXES = ("/api/patients", "/api/alerts", "/api/attention")

_METHOD_TO_ACTION = {
    "GET": "READ",
    "HEAD": "READ",
    "POST": "CREATE",
    "PUT": "UPDATE",
    "PATCH": "UPDATE",
    "DELETE": "DELETE",
}


def _is_resource_id(segment: str) -> bool:
    """True if a URL path segment is a record id — an integer or a UUID PK."""
    if segment.isdigit():
        return True
    try:
        uuid.UUID(segment)
    except ValueError:
        return False
    return True


def _client_ip(request: HttpRequest) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


class AuditLogMiddleware:
    """Logs access to PHI-touching endpoints."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        try:
            self._record(request)
        except Exception:  # noqa: BLE001 - auditing must never break the request
            logger.exception("Failed to write audit log for %s", request.path)
        return response

    def _record(self, request: HttpRequest) -> None:
        path = request.path
        if not path.startswith(_PHI_PREFIXES):
            return
        action = _METHOD_TO_ACTION.get(request.method)
        if action is None:
            return

        from momcare_platform.core.organization.models import AuditLog

        resource, resource_id = self._parse_path(path)
        user = getattr(request, "user", None)
        AuditLog.objects.create(
            user=user if (user and user.is_authenticated) else None,
            action=action,
            resource=resource[:100],
            resource_id=resource_id[:100],
            ip_address=_client_ip(request),
            endpoint=path[:255],
        )

    @staticmethod
    def _parse_path(path: str) -> tuple[str, str]:
        """``/api/patients/<id>/`` -> ("patients", "<id>").

        The id segment may be an integer or a UUID (model PKs are UUIDs).
        """
        parts = [p for p in path.strip("/").split("/") if p]
        if parts and parts[0] == "api":
            parts = parts[1:]
        if not parts:
            return "", ""
        resource_id = ""
        if _is_resource_id(parts[-1]):
            resource_id = parts[-1]
            parts = parts[:-1]
        return "/".join(parts), resource_id


class DisplayTimezoneMiddleware:
    """Activate the logged-in user's timezone so DRF localizes datetimes.

    Authenticates the request with the JWT authenticator itself (DRF resolves
    ``request.user`` only inside the view, too late for middleware). Any failure
    or anonymous request falls through to UTC; the view still enforces real auth.
    Always deactivates in ``finally`` so an activated zone never leaks to the next
    request on a reused worker thread.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.utils import timezone

        from momcare_platform.core.common.timezones import resolve_display_tz

        tz = None
        user_ctx_token = None
        try:
            from rest_framework_simplejwt.authentication import JWTAuthentication

            result = JWTAuthentication().authenticate(request)
            if result is not None:
                user = result[0]
                tz = resolve_display_tz(user)
                user_ctx_token = user_id_ctx.set(str(user.id))
        except Exception:  # noqa: BLE001 - display tz is best-effort; never break the request
            tz = None

        if tz is not None:
            timezone.activate(tz)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
            if user_ctx_token is not None:
                user_id_ctx.reset(user_ctx_token)


class NoCacheAPIMiddleware:
    """Stop browsers/back-forward cache from replaying stale ``/api/`` responses."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path.startswith("/api/") and not response.has_header("Cache-Control"):
            add_never_cache_headers(response)
        return response


class RequestLoggingMiddleware:
    """One JSON access-log line per request — method, path, status, duration,
    resolved view, client IP. Fires via ``try/finally`` so a request that
    raises still gets logged, with status defaulting to 500.

    Never logs request/response bodies (PHI risk).

    Must be listed after ``DisplayTimezoneMiddleware`` in ``MIDDLEWARE``: that
    middleware clears ``user_id_ctx`` in its own ``finally``, so this
    middleware must run from a position nested inside it, not outside it.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        start = time.monotonic()
        response: HttpResponse | None = None
        try:
            response = self.get_response(request)
            response["X-Request-Id"] = request_id
            return response
        finally:
            try:
                duration_ms = round((time.monotonic() - start) * 1000)
                status = response.status_code if response is not None else 500
                logger.info(
                    '"%s %s" %s %sms',
                    request.method,
                    request.get_full_path(),
                    status,
                    duration_ms,
                    extra={
                        "method": request.method,
                        "path": request.get_full_path(),
                        "status": status,
                        "duration_ms": duration_ms,
                        "ip": _client_ip(request),
                        "view": self._resolve_view(request),
                    },
                )
            finally:
                request_id_ctx.reset(token)

    @staticmethod
    def _resolve_view(request: HttpRequest) -> str | None:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return None
        view_class = getattr(match.func, "view_class", None)
        if view_class is not None:
            return f"{view_class.__module__}.{view_class.__qualname__}"
        return f"{match.func.__module__}.{match.func.__qualname__}"


class AdminRLSBypassMiddleware:
    """Lets a signed-in Django admin user see across every hospital.

    Row-level security is scoped by ``OrganizationScopedQuerysetMixin`` at the
    DRF view layer (``core/common/scoping.py``), which the admin site does not
    use — its querysets go straight through the ORM. Without this, a platform
    administrator working in ``/admin/`` would see no rows at all once RLS is
    enforced by a non-bypassing database role, because no per-request
    organization was ever set for that path and nothing else grants access.

    Scoped tightly on purpose: only requests under ``settings.ADMIN_URL``, from
    a user who is already authenticated. It never touches ``/api/``, so a
    hospital's own requests can never pick up bypass by sharing this
    middleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings as dj_settings  # noqa: PLC0415

        admin_url = dj_settings.ADMIN_URL
        admin_prefix = admin_url if admin_url.startswith("/") else f"/{admin_url}"

        if (
            request.path.startswith(admin_prefix)
            and getattr(request, "user", None)
            and request.user.is_authenticated
        ):
            from momcare_platform.core.common.rls import bypass_rls  # noqa: PLC0415

            # bypass_rls() opens its own transaction, so nothing else here does.
            with bypass_rls():
                return self.get_response(request)

        return self.get_response(request)
