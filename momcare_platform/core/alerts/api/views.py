"""Alert endpoints.

Scoped like everything else: an alert is reached through its pregnancy's
hospital, so another tenant's alert resolves to nothing rather than being found
and then refused.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.alerts.api.serializers import (
    AlertDetailSerializer,
    AlertSerializer,
    ResolveSerializer,
)
from momcare_platform.core.alerts.models import Alert
from momcare_platform.core.alerts.services import acknowledge_alert, resolve_alert
from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsClinician, IsHospitalStaff
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin

NO_HOSPITAL = {"detail": "This account is not attached to a hospital."}

# Severity first, then oldest first inside a level: the alert that has waited
# longest at the worst level is the one somebody should open.
SEVERITY_ORDER = {"critical": 0, "high": 1, "moderate": 2}


class AlertScopedView(OrganizationScopedQuerysetMixin, APIView):
    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "pregnancy__patient__location__organization"

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def alerts(self):
        return self.scope_to_organization(Alert.objects.all()).select_related(
            "pregnancy__patient",
            "pregnancy__assigned_staff__user",
            "assessment",
            "acknowledged_by",
        )

    def get_alert_or_404(self, alert_id):
        try:
            return self.alerts().get(pk=alert_id), None
        except (Alert.DoesNotExist, DjangoValidationError, ValueError):
            return None, Response(
                {"detail": "Alert not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class AlertListView(AlertScopedView):
    """This hospital's alerts.

    Defaults to the live ones. Resolved alerts are still readable — the record
    of what happened is the point of keeping them — but they are asked for
    explicitly rather than crowding the list somebody is working from.
    """

    def _scope_to_assigned(self, queryset, request):
        """``?assigned_to=me`` — same semantics as the patient list's own
        version (core/patients/api/views.py), reused here rather than
        duplicated logic diverging over time. Alert visibility follows
        assignment, not location — see the master plan's own §16 reasoning:
        a nurse legitimately needs to see an alert for a patient outside
        their usual ward if they're on that patient's care team.
        """
        if request.query_params.get("assigned_to") != "me":
            return queryset

        staff = getattr(request.user, "staff", None)
        if staff is None:
            return queryset.none()

        role = request.user.role_code
        if role == "provider":
            return queryset.filter(
                Q(pregnancy__assigned_staff=staff)
                | Q(
                    pregnancy__care_team_memberships__staff=staff,
                    pregnancy__care_team_memberships__role="provider",
                    pregnancy__care_team_memberships__is_active=True,
                ),
            ).distinct()
        if role in ("nurse", "care_manager"):
            return queryset.filter(
                pregnancy__care_team_memberships__staff=staff,
                pregnancy__care_team_memberships__role=role,
                pregnancy__care_team_memberships__is_active=True,
            ).distinct()
        # hospital_admin and anyone else: same honest-empty-result choice as
        # the patient list — "my alerts" isn't a concept that applies to them.
        return queryset.none()

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        requested = request.query_params.get("status", "live")
        queryset = self._scope_to_assigned(self.alerts(), request)
        if requested == "live":
            queryset = queryset.filter(status__in=Alert.LIVE_STATUSES)
        elif requested in dict(Alert.STATUS_CHOICES):
            queryset = queryset.filter(status=requested)

        rows = sorted(
            queryset,
            key=lambda a: (SEVERITY_ORDER.get(a.level, 9), a.raised_at),
        )

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        body = paginator.get_paginated_response(AlertSerializer(page, many=True).data)
        # The unanswered count drives the notification badge. Derived here so
        # the portal never has to fetch the whole list to render a number.
        body.data["unacknowledged"] = sum(1 for a in rows if a.status == Alert.STATUS_OPEN)
        return body


class AlertDetailView(AlertScopedView):
    def get(self, request, alert_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        alert, missing = self.get_alert_or_404(alert_id)
        if missing:
            return missing
        return Response(AlertDetailSerializer(alert).data)


class AlertAcknowledgeView(AlertScopedView):
    """Record that a named person has seen this. Stops the escalation clock.

    Clinicians only. Stopping the clock is a clinical act: the ladder climbs
    *towards* the hospital administrator precisely because nobody nearer has
    answered, so letting the administrator acknowledge would let the ladder be
    ended by the person it was escalating to, with no clinical judgement made
    and the record saying otherwise.
    """

    permission_classes = [IsAuthenticated, IsClinician]

    def post(self, request, alert_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        alert, missing = self.get_alert_or_404(alert_id)
        if missing:
            return missing

        if alert.status == Alert.STATUS_RESOLVED:
            return Response(
                {"detail": "This alert is already closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(AlertDetailSerializer(acknowledge_alert(alert, request.user)).data)


class AlertResolveView(AlertScopedView):
    """Close the episode. Separate from acknowledging on purpose: seeing an
    alert and finishing with the patient are different claims.

    Clinicians only, for the same reason and one more. "Recovered" and "handled"
    are statements about a patient, not about paperwork. And an alert nobody
    ever answered is evidence about how this hospital is covered — an
    administrator who could close it would be tidying away the one signal that
    should prompt them to fix the rota.
    """

    permission_classes = [IsAuthenticated, IsClinician]

    def post(self, request, alert_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        alert, missing = self.get_alert_or_404(alert_id)
        if missing:
            return missing

        if not alert.is_live:
            return Response(
                {"detail": "This alert is already closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        resolved = resolve_alert(alert, request.user, serializer.validated_data["resolution"])
        return Response(AlertDetailSerializer(resolved).data)
