"""Health check endpoint for load balancers and uptime monitors.

Plain Django view (not DRF) so it bypasses the JWT/IsAuthenticated default and
stays dependency-free. Verifies database connectivity so the check reflects
readiness, not just that the process is up.
"""

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def health_check(request):
    """Return 200 when the app can reach the database, 503 otherwise."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 — any DB failure means not ready
        return JsonResponse({"status": "error", "database": "down"}, status=503)

    return JsonResponse({"status": "ok", "database": "ok"})
