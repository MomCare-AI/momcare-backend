"""What a hospital administrator sees on opening the portal.

Two questions this answers, both from real rows:

  How is my ward right now?    - the spread of risk across active pregnancies
  What has been happening?     - who did what, from the audit log

Neither invents a number. Where a figure would have to be guessed, the shape it
is missing from is reported instead - "not assessed" is its own category, not
folded into stable, because a patient nobody has measured is not a patient who
is well.
"""

from django.conf import settings
from django.db.models import OuterRef, Subquery
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.permissions import user_role_code
from momcare_platform.core.organization.models import AuditLog
from momcare_platform.core.patients.models import Pregnancy

# Ordered worst-first, which is also the order the interface renders them in.
RISK_LEVELS = ["critical", "high", "moderate", "stable"]

ACTIVITY_LIMIT = 12


def _risk_distribution(organization) -> dict:
    """Count active pregnancies by their current risk level.

    One query. The latest assessment per pregnancy comes from a correlated
    subquery rather than a loop, because the obvious implementation - iterate
    the pregnancies and read ``risk_assessments.first()`` - costs one round trip
    per patient and this runs on every dashboard load.
    """
    from momcare_platform.core.monitoring.models import RiskAssessment  # noqa: PLC0415

    latest_level = (
        RiskAssessment.objects.filter(pregnancy=OuterRef("pk"))
        .order_by("-assessed_at")
        .values("level")[:1]
    )

    rows = (
        Pregnancy.objects.filter(
            status=Pregnancy.STATUS_ACTIVE,
            patient__location__organization=organization,
        )
        .annotate(current_level=Subquery(latest_level))
        .values_list("current_level", flat=True)
    )

    counts = {level: 0 for level in RISK_LEVELS}
    # Null means no assessment has ever been written for this pregnancy. Kept
    # separate on purpose: reporting it as stable would be the dashboard
    # inventing reassurance nobody measured.
    counts["not_assessed"] = 0

    for level in rows:
        key = level if level in counts else "not_assessed"
        counts[key] += 1

    counts["total"] = sum(counts[k] for k in [*RISK_LEVELS, "not_assessed"])
    counts["needing_attention"] = sum(counts[k] for k in ["critical", "high", "moderate"])
    return counts


def _recent_activity(organization) -> list[dict]:
    """The last few recorded actions at this hospital.

    Read from the PHI audit log, which already records every request that
    touched a patient, an alert or the attention queue. Showing it back is
    partly the point: a clinical system that logs access and never surfaces it
    is asking to be taken on trust.
    """
    entries = (
        AuditLog.objects.filter(user__organization=organization)
        .select_related("user")
        .order_by("-timestamp")[:ACTIVITY_LIMIT]
    )

    return [
        {
            "action": entry.action,
            "resource": entry.resource,
            "actor": entry.user.get_full_name() if entry.user else "",
            "at": entry.timestamp,
        }
        for entry in entries
    ]


class DashboardSummaryView(APIView):
    """Aggregates for the portal overview, scoped to the caller's own hospital.

    Like every other tenant-owned read, the hospital comes from the
    authenticated user rather than from the request, so there is no identifier
    a caller could change to see another hospital's ward.

    Deliberately just IsAuthenticated at the class level, matching
    MyOrganizationView, plus one explicit role check inside get() rather than
    IsHospitalStaff as a permission class. IsHospitalStaff would reject a
    platform_admin with 403 before this view's own organization-is-None check
    ever ran, turning a clear "you have no hospital" 404 into an opaque 403.
    But dropping the role check entirely would let a patient - who does have
    an organization - see this hospital's whole risk distribution and its
    staff's PHI-access audit trail. Excluding only the patient role by hand
    keeps both properties true at once.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if user_role_code(request.user) == settings.ROLE_PATIENT:
            return Response(
                {"detail": "This view is for hospital staff, not patient accounts."},
                status=status.HTTP_403_FORBIDDEN,
            )

        organization = request.user.organization
        if organization is None:
            return Response(
                {"detail": "This account is not attached to a hospital."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "risk": _risk_distribution(organization),
                "activity": _recent_activity(organization),
            },
        )
