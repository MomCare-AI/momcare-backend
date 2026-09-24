from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.mail import send_staff_invitation
from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.locations.models import Location
from momcare_platform.core.staff.api.serializers import (
    SecondaryProviderSerializer,
    StaffMemberSerializer,
    StaffOnboardSerializer,
    StaffProfileUpdateSerializer,
    StaffUpdateSerializer,
)
from momcare_platform.core.staff.models import SecondaryProvider, Staff
from momcare_platform.core.staff.services import (
    StaffError,
    can_manage_staff,
    deactivate_staff,
    delete_staff,
    onboard_staff,
    reactivate_staff,
)


def _set_password_url(user) -> str:
    """The one-time link a newly-invited staff member follows to choose their
    password. Same route the reset flow already uses — one screen, one token
    scheme, one thing to keep working."""
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return f"{settings.FRONTEND_URL.rstrip('/')}/reset-password/{uid}/{token}"


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

    Read: any hospital-side role. Write (onboarding): hospital_admin (any
    location in the hospital) or a location's own manager (only into
    location(s) they themselves manage) — mirrors the Admin-or-own-manager
    split already used for Locations.
    """

    organization_lookup = "user__organization"

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        staff = (
            self.scope_to_organization(Staff.objects.all())
            .select_related("user", "user__role")
            .prefetch_related("user__locations")
            .order_by("user__first_name", "user__last_name")
        )
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(staff, request, view=self)
        return paginator.get_paginated_response(
            StaffMemberSerializer(page, many=True, context={"request": request}).data,
        )

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        is_admin = request.user.role_code == settings.ROLE_HOSPITAL_ADMIN
        if not is_admin and not Location.objects.filter(organization=org, location_manager=request.user).exists():
            return Response(
                {"detail": "Only a hospital administrator or a location manager can onboard staff."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = StaffOnboardSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        staff = onboard_staff(
            organization=org,
            email=data["email"],
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            phone=data.get("phone", ""),
            role_code=data["role_code"],
            locations=data.get("locations") or [],
        )

        # The same token machinery as "forgot password", reused deliberately
        # rather than inventing a second one: it is already single-use (the
        # token is derived partly from the password hash, so setting a
        # password invalidates it) and already time-limited.
        send_staff_invitation(staff.user, _set_password_url(staff.user), org)

        return Response(
            StaffMemberSerializer(staff, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class StaffScopedView(HospitalPortalView):
    """Base for one-staff-member endpoints — tenant-scoped lookup shared by
    detail/update/deactivate/reactivate/delete/assignment-status."""

    organization_lookup = "user__organization"

    def get_staff_or_404(self, staff_id):
        """Scope first, then look up — another hospital's staff id resolves
        to nothing rather than being found and then refused."""
        try:
            return (
                self.scope_to_organization(Staff.objects.all()).select_related("user", "user__role").get(pk=staff_id)
            ), None
        except Staff.DoesNotExist:
            return None, Response({"detail": "Staff member not found."}, status=status.HTTP_404_NOT_FOUND)


class StaffProfileView(StaffScopedView):
    """One staff member's profile.

    Read: any hospital staff. Write: the staff member themselves may update
    their own credentialing fields (photo/qualifications/specialty/etc, via
    ``StaffProfileUpdateSerializer``); hospital_admin, or a manager of (at
    least one of) this staff member's locations, may additionally update
    role/locations/password (via ``StaffUpdateSerializer``) — mirrors the
    same Admin-or-own-manager split used for onboarding.
    """

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, staff_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        member, missing = self.get_staff_or_404(staff_id)
        if missing:
            return missing
        return Response(StaffMemberSerializer(member, context={"request": request}).data)

    def patch(self, request, staff_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        member, missing = self.get_staff_or_404(staff_id)
        if missing:
            return missing

        is_self = member.user_id == request.user.id
        can_manage = can_manage_staff(request.user, member)
        if not (is_self or can_manage):
            return Response(
                {"detail": "You can only edit your own profile, or staff you manage."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer_class = StaffUpdateSerializer if can_manage else StaffProfileUpdateSerializer
        serializer = serializer_class(member, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(StaffMemberSerializer(member, context={"request": request}).data)

    def delete(self, request, staff_id):
        """Hard-delete — hospital_admin only, same as onboarding a new
        hospital_admin, and only once the staff member is already
        deactivated (see ``staff.services.delete_staff``). Deletes the
        underlying account outright, not just the employment record."""
        _, error = self.hospital_or_error(request)
        if error:
            return error
        if request.user.role_code != settings.ROLE_HOSPITAL_ADMIN:
            return Response(
                {"detail": "Only a hospital administrator can delete a staff member."},
                status=status.HTTP_403_FORBIDDEN,
            )
        member, missing = self.get_staff_or_404(staff_id)
        if missing:
            return missing

        try:
            delete_staff(member)
        except StaffError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


class StaffAssignmentStatusView(StaffScopedView):
    """Whether this staff member can be deactivated right now, and why not
    if not — the same "don't make the frontend guess and fail" pattern
    Locations already uses."""

    def get(self, request, staff_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        member, missing = self.get_staff_or_404(staff_id)
        if missing:
            return missing

        count = member.current_patient_count
        return Response(
            {
                "has_active_patients": count > 0,
                "active_patient_count": count,
                "message": (
                    f"This staff member can't be deactivated because {count} patient(s) "
                    "are currently assigned to them. Reassign those patients first."
                    if count
                    else "This staff member has no assigned patients and can be safely deactivated."
                ),
            },
        )


class StaffDeactivateView(StaffScopedView):
    """Deactivate — never delete. hospital_admin or a manager of (at least
    one of) this staff member's locations, blocked only by the
    zero-active-patients guard (no approval step) — same shape as
    Locations' own deactivate."""

    def post(self, request, staff_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        member, missing = self.get_staff_or_404(staff_id)
        if missing:
            return missing
        if not can_manage_staff(request.user, member):
            return Response(
                {"detail": "Only a hospital administrator or a manager of this staff member can deactivate them."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            deactivate_staff(member, by=request.user, reason=request.data.get("reason", ""))
        except StaffError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(StaffMemberSerializer(member, context={"request": request}).data)


class StaffReactivateView(StaffScopedView):
    """Restore a previously deactivated staff member. hospital_admin or a
    manager of (at least one of) their locations — unlike Locations'
    reactivate, which stays hospital_admin-only; Neuro_RPM's own Staff
    reactivate is not admin-restricted either, only its hard-delete is."""

    def post(self, request, staff_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        member, missing = self.get_staff_or_404(staff_id)
        if missing:
            return missing
        if not can_manage_staff(request.user, member):
            return Response(
                {"detail": "Only a hospital administrator or a manager of this staff member can reactivate them."},
                status=status.HTTP_403_FORBIDDEN,
            )

        reactivate_staff(member)
        return Response(StaffMemberSerializer(member, context={"request": request}).data)


class SecondaryProviderBaseView(HospitalPortalView):
    """Scoping and the write rule, shared by the list and detail views.

    Deliberately a sibling base rather than the detail view subclassing the
    list view: the two disagree about what ``get`` takes (one a provider id,
    one not), so inheriting one from the other would mean a subclass that
    cannot stand in for its parent.
    """

    organization_lookup = "organization"

    def providers(self):
        return self.scope_to_organization(SecondaryProvider.objects.all())

    def can_write(self, user) -> bool:
        return user.role_code in (settings.ROLE_HOSPITAL_ADMIN, settings.ROLE_CARE_MANAGER)


class SecondaryProviderListCreateView(SecondaryProviderBaseView):
    """This hospital's external clinicians — the referral list.

    Read: any hospital-side role. Write: hospital_admin or care_manager —
    the same split the reference platform uses (Admin | CareManager), since
    curating the referral list is administrative work, not a clinical
    judgement a nurse makes mid-shift.
    """

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        queryset = self.providers()
        search = request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(email__icontains=search) | Q(phone__icontains=search),
            )

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset.order_by("name", "id"), request, view=self)
        return paginator.get_paginated_response(SecondaryProviderSerializer(page, many=True).data)

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        if not self.can_write(request.user):
            return Response(
                {"detail": "Only a hospital administrator or care manager can add a secondary provider."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SecondaryProviderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # organization comes from the caller, never the body.
        provider = serializer.save(organization=org)
        return Response(SecondaryProviderSerializer(provider).data, status=status.HTTP_201_CREATED)


class SecondaryProviderDetailView(SecondaryProviderBaseView):
    """Read, correct, or remove one external clinician."""

    def get_provider_or_404(self, provider_id):
        """Scope first, then look up — another hospital's referral contact
        resolves to nothing rather than being found and refused."""
        try:
            return self.providers().get(pk=provider_id), None
        except SecondaryProvider.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response(
                {"detail": "Secondary provider not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

    def get(self, request, provider_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        provider, missing = self.get_provider_or_404(provider_id)
        return missing or Response(SecondaryProviderSerializer(provider).data)

    def patch(self, request, provider_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        provider, missing = self.get_provider_or_404(provider_id)
        if missing:
            return missing
        if not self.can_write(request.user):
            return Response(
                {"detail": "Only a hospital administrator or care manager can edit a secondary provider."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = SecondaryProviderSerializer(provider, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, provider_id):
        """A real delete — unlike Patient or Staff, this is a contact-list
        entry, not a clinical record. Patients referencing it keep their own
        data and simply lose the link (``SET_NULL``), so nothing clinical is
        destroyed by removing a name from the list.
        """
        _, error = self.hospital_or_error(request)
        if error:
            return error
        provider, missing = self.get_provider_or_404(provider_id)
        if missing:
            return missing
        if not self.can_write(request.user):
            return Response(
                {"detail": "Only a hospital administrator or care manager can remove a secondary provider."},
                status=status.HTTP_403_FORBIDDEN,
            )

        provider.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
