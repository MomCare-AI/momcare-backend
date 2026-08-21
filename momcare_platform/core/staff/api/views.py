from django.conf import settings
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.mail import send_staff_invitation
from momcare_platform.core.common.permissions import IsHospitalAdmin
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.staff.api.serializers import (
    InviteAcceptSerializer,
    InvitePreviewSerializer,
    StaffInviteCreateSerializer,
    StaffInviteSerializer,
    StaffMemberSerializer,
)
from momcare_platform.core.staff.models import Staff, StaffInvite
from momcare_platform.core.staff.services import InviteError, accept_invite


class HospitalPortalView(OrganizationScopedQuerysetMixin, APIView):
    """Base for endpoints that serve one hospital's own portal.

    Scoping goes through ``scope_to_organization`` rather than a hand-written
    ``.filter()``, so isolation is structural: a view that forgets to scope is
    the classic way tenant data leaks, and the mixin is the codebase's answer
    to that (see ``common/scoping.py``).

    A caller with no hospital gets 404 rather than the mixin's unrestricted
    branch. Platform admins are the only users without an organization, and
    they work through the Django admin — a cross-tenant staff list is not
    something a hospital portal endpoint should ever return.
    """

    permission_classes = [IsAuthenticated]
    no_hospital_detail = "This account is not attached to a hospital."

    def hospital_or_error(self, request):
        """Return (organization, None) or (None, 404 response)."""
        org = request.user.organization
        if org is None:
            return None, Response(
                {"detail": self.no_hospital_detail},
                status=status.HTTP_404_NOT_FOUND,
            )
        return org, None


class StaffListView(HospitalPortalView):
    """The signed-in user's own hospital team.

    Readable by any hospital-side role; only admins can change it.
    """

    organization_lookup = "user__organization"

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        staff = (
            self.scope_to_organization(Staff.objects.all())
            .select_related("user", "user__role")
            .order_by("user__first_name", "user__last_name")
        )
        return Response(StaffMemberSerializer(staff, many=True).data)


class StaffInviteListCreateView(HospitalPortalView):
    """List this hospital's invitations, or create a new one."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        invites = (
            self.scope_to_organization(StaffInvite.objects.all())
            .select_related("role", "invited_by")
            .order_by("-created_at")
        )
        return Response(StaffInviteSerializer(invites, many=True).data)

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error
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

        emailed = False
        if serializer.validated_data.get("send_email", True):
            accept_url = f"{settings.FRONTEND_URL.rstrip('/')}/invite/{invite.token}"
            emailed = send_staff_invitation(invite, accept_url)

        payload = StaffInviteSerializer(invite).data
        payload["emailed"] = emailed
        return Response(payload, status=status.HTTP_201_CREATED)


class StaffInviteRevokeView(HospitalPortalView):
    """Revoke a pending invitation. The row is kept — revoking is recorded, not erased."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def post(self, request, invite_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        # Scope first, then look up: another hospital's invite id resolves to
        # nothing rather than being found and then rejected.
        try:
            invite = self.scope_to_organization(StaffInvite.objects.all()).get(pk=invite_id)
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
