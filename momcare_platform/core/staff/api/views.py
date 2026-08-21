from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.permissions import IsHospitalAdmin
from momcare_platform.core.staff.api.serializers import (
    InviteAcceptSerializer,
    InvitePreviewSerializer,
    StaffInviteCreateSerializer,
    StaffInviteSerializer,
    StaffMemberSerializer,
)
from momcare_platform.core.staff.models import Staff, StaffInvite
from momcare_platform.core.staff.services import InviteError, accept_invite


class StaffListView(APIView):
    """The signed-in user's own hospital team.

    Scoped through ``user__organization`` so the queryset can only ever contain
    the caller's own tenant — there is no organization id in the URL to tamper
    with. Readable by any hospital-side role; only admins can change it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response({"detail": "This account is not attached to a hospital."}, status=404)
        staff = (
            Staff.objects.filter(user__organization=org)
            .select_related("user", "user__role")
            .order_by("user__first_name", "user__last_name")
        )
        return Response(StaffMemberSerializer(staff, many=True).data)


class StaffInviteListCreateView(APIView):
    """List this hospital's invitations, or create a new one."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def _organization(self, request):
        return request.user.organization

    def get(self, request):
        org = self._organization(request)
        if org is None:
            return Response({"detail": "This account is not attached to a hospital."}, status=404)
        invites = (
            StaffInvite.objects.filter(organization=org)
            .select_related("role", "invited_by")
            .order_by("-created_at")
        )
        return Response(StaffInviteSerializer(invites, many=True).data)

    def post(self, request):
        org = self._organization(request)
        if org is None:
            return Response({"detail": "This account is not attached to a hospital."}, status=404)
        if not org.can_authenticate:
            return Response(
                {"detail": "Your hospital is not active, so invitations cannot be sent."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = StaffInviteCreateSerializer(
            data=request.data,
            context={"organization": org, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        invite = serializer.save()
        return Response(StaffInviteSerializer(invite).data, status=status.HTTP_201_CREATED)


class StaffInviteRevokeView(APIView):
    """Revoke a pending invitation. The row is kept — revoking is recorded, not erased."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def post(self, request, invite_id):
        org = request.user.organization
        try:
            invite = StaffInvite.objects.get(pk=invite_id, organization=org)
        except StaffInvite.DoesNotExist:
            return Response({"detail": "Invitation not found."}, status=status.HTTP_404_NOT_FOUND)

        if invite.accepted_at is not None:
            return Response(
                {"detail": "This invitation has already been accepted."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if invite.revoked_at is None:
            invite.revoked_at = timezone.now()
            invite.save(update_fields=["revoked_at", "updated_at"])
        return Response(StaffInviteSerializer(invite).data)


@method_decorator(csrf_exempt, name="dispatch")
class InviteDetailView(APIView):
    """Public: what the recipient sees when they open their invitation link."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, token):
        try:
            invite = StaffInvite.objects.select_related("organization", "role", "invited_by").get(token=token)
        except StaffInvite.DoesNotExist:
            return Response({"detail": "This invitation link is not valid."}, status=status.HTTP_404_NOT_FOUND)

        if not invite.is_pending:
            return Response(
                {"detail": "This invitation is no longer usable.", "invite_status": invite.status},
                status=status.HTTP_410_GONE,
            )
        return Response(InvitePreviewSerializer(invite).data)


@method_decorator(csrf_exempt, name="dispatch")
class InviteAcceptView(APIView):
    """Public: the recipient sets their name and password and joins the hospital."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request, token):
        serializer = InviteAcceptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = accept_invite(
                token=token,
                password=serializer.validated_data["password"],
                first_name=serializer.validated_data["first_name"],
                last_name=serializer.validated_data["last_name"],
            )
        except InviteError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "detail": "Your account is ready. You can now sign in.",
                "email": user.email,
                "organization_name": user.organization.name,
            },
            status=status.HTTP_201_CREATED,
        )
