from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsHospitalAdmin, IsHospitalStaff
from momcare_platform.core.organization.api.serializers import (
    AuditLogSerializer,
    NotificationSerializer,
    OrganizationConfidenceThresholdSerializer,
    OrganizationDeactivationRequestCreateSerializer,
    OrganizationDeactivationRequestSerializer,
    OrganizationSerializer,
)
from momcare_platform.core.organization.models import AuditLog, Notification
from momcare_platform.core.organization.services import DeactivationRequestError, request_deactivation

NO_HOSPITAL = {"detail": "This account is not attached to a hospital."}


class MyOrganizationView(APIView):
    """The signed-in user's own hospital.

    Scoped by ``request.user.organization`` rather than by a URL id, so there is
    no tenant identifier a caller could tamper with to read another hospital.
    Platform admins have no organization and get 404 here — they work through
    the admin, not a hospital dashboard.

    - ``GET``   — any authenticated member of the hospital.
    - ``PATCH`` — hospital_admin only. Accepts the hospital's ordinary profile
      fields (name, contact, address, timezone, date_format,
      established_date) — see ``OrganizationSerializer`` for exactly which
      fields stay read-only (the review state and the derived/computed
      ones). The confidence threshold is a clinical-safety setting in a
      "narrow on purpose" spirit and gets its own endpoint rather than
      living here — see ``OrganizationConfidenceThresholdView``.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return Response(OrganizationSerializer(org, context={"request": request}).data)

    def patch(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        if not IsHospitalAdmin().has_permission(request, self):
            return Response(
                {"detail": "Only a hospital administrator can update this."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = OrganizationSerializer(org, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class OrganizationDeactivationRequestView(APIView):
    """A hospital's own request to close its account.

    hospital_admin only, both directions — this is account-closure territory,
    not ordinary profile reading.

    - ``GET``  — the hospital's most recent request (whatever its status), or
      404 if none has ever been made. One call tells the frontend everything
      it needs to render "no request" / "pending review" / "declined,
      see note" without a second endpoint to poll.
    - ``POST`` — make a new one. Refused with 400 while one is already
      pending; the hospital gets a plain answer instead of a duplicate row
      or a raw database error.

    Approving or dismissing a request happens on the platform-admin side —
    Django admin today (``OrganizationDeactivationRequestAdmin``) — never
    from here. This view can only ever create the ask, never resolve it.
    """

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        latest = org.deactivation_requests.order_by("-created_at").first()
        if latest is None:
            return Response(
                {"detail": "No deactivation request has been made for this hospital."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(OrganizationDeactivationRequestSerializer(latest).data)

    def post(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        serializer = OrganizationDeactivationRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            deactivation_request = request_deactivation(
                org,
                by=request.user,
                reason=serializer.validated_data["reason"],
            )
        except DeactivationRequestError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            OrganizationDeactivationRequestSerializer(deactivation_request).data,
            status=status.HTTP_201_CREATED,
        )


class OrganizationConfidenceThresholdView(APIView):
    """Change this hospital's own override of the model confidence threshold.

    Only hospital_admin may change it — the same clinical-safety reasoning
    that keeps every other setting on ``MyOrganizationView`` read-only.
    Sending ``{"confidence_threshold": null}`` clears the override and
    reverts this hospital to following the platform default live — see
    ``Organization.effective_confidence_threshold``.
    """

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def patch(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        serializer = OrganizationConfidenceThresholdSerializer(org, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(OrganizationSerializer(org, context={"request": request}).data)


class OrganizationAuditLogView(APIView):
    """This hospital's own PHI-access trail — who looked at what, when.

    hospital_admin only, same clinical-safety-adjacent restriction as the
    confidence threshold above: this is a compliance record about staff
    conduct, not a general profile detail. Scoped through
    ``request.user.organization`` — the same tamper-proof pattern used
    throughout this file, never a URL id a caller could substitute.

    Read-only, always newest first — an access trail has exactly one
    meaningful order. ``?action=``/``?resource=`` narrow it (e.g. every
    DELETE, or every touch of a specific patient); there is no free-text
    ``?ordering=`` to offer, so this deliberately does not use
    StableOrderingFilter.
    """

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        queryset = AuditLog.objects.filter(user__organization=org).select_related("user")

        action = request.query_params.get("action", "").strip().upper()
        if action:
            queryset = queryset.filter(action=action)
        resource = request.query_params.get("resource", "").strip()
        if resource:
            queryset = queryset.filter(resource__iexact=resource)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset.order_by("-timestamp"), request, view=self)
        return paginator.get_paginated_response(AuditLogSerializer(page, many=True).data)


class OrganizationNotificationsView(APIView):
    """The hospital's own "things that need attention" feed -- what the
    bell icon shows. Visible to any hospital-side role, matching who can
    already see the events these notifications point at (e.g. any
    hospital-side role can already read ``/patient-requests/``).

    No email, no push -- these exist purely for a client to poll or show
    on login. ``?is_read=true/false`` narrows the list; the response
    always carries a top-level ``unread_count`` for the badge, computed
    once per request rather than requiring the client to count the page
    itself (which would be wrong the moment the list is paginated).
    """

    permission_classes = [IsAuthenticated, IsHospitalStaff]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        queryset = Notification.objects.filter(organization=org)

        is_read = request.query_params.get("is_read")
        if is_read is not None:
            queryset = queryset.filter(is_read=is_read.lower() in ("1", "true", "yes"))

        unread_count = Notification.objects.filter(organization=org, is_read=False).count()

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset.order_by("-created_at"), request, view=self)
        body = paginator.get_paginated_response(NotificationSerializer(page, many=True).data)
        body.data["unread_count"] = unread_count
        return body


class NotificationMarkReadView(APIView):
    """Mark one notification as seen -- clears it off the badge count."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]

    def post(self, request, notification_id):
        org = request.user.organization
        if org is None:
            return Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)

        try:
            notification = Notification.objects.get(organization=org, pk=notification_id)
        except Notification.DoesNotExist, DjangoValidationError, ValueError:
            return Response({"detail": "Notification not found."}, status=status.HTTP_404_NOT_FOUND)

        notification.mark_read()
        return Response(NotificationSerializer(notification).data)
