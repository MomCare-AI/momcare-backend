"""Location endpoints — a hospital managing its own sites.

Every queryset here is scoped through ``organization`` (a direct FK, same as
``Device``) by the shared tenancy mixin. There is no platform-admin approval
anywhere in this app, on purpose: a location is entirely internal to one
hospital.

Write access follows Neuro_RPM's ``Admin | LocationAdmin`` split
(``core/common/permissions.py``, ``core/locations/api/views.py`` there):
hospital_admin manages every location unconditionally; a location's own
``location_manager`` manages just that one (update, deactivate, move
patients out of it). MomCare has no separate "location admin" flag the way
Neuro_RPM does — the compulsory ``location_manager`` FK already names
exactly one person per location, so that FK *is* the designation.
Create and reactivate stay hospital_admin-only, same as Neuro_RPM: standing
up a new site or resurrecting a closed one is a hospital-level call, not an
operational one.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsHospitalAdmin, IsHospitalStaff
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.locations.api.serializers import LocationSerializer, MoveLocationPatientsSerializer
from momcare_platform.core.locations.models import Location
from momcare_platform.core.locations.services import (
    LocationError,
    create_location,
    deactivate_location,
    delete_location,
    move_patients_to_location,
    reactivate_location,
)

NO_HOSPITAL = {"detail": "This account is not attached to a hospital."}


class LocationScopedView(OrganizationScopedQuerysetMixin, APIView):
    """Base for location endpoints — tenant-scoped, hospital staff only."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "organization"

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def locations(self):
        return self.scope_to_organization(Location.objects.all())

    def get_location_or_404(self, location_id):
        """Scope first, then look up — another hospital's location resolves
        to nothing rather than being found and then refused."""
        try:
            return self.locations().select_related("organization", "location_manager").get(pk=location_id), None
        except Location.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Location not found."}, status=status.HTTP_404_NOT_FOUND)

    def can_manage(self, request, location) -> bool:
        """hospital_admin manages every location; this location's own
        manager manages just this one — see the module docstring."""
        if IsHospitalAdmin().has_permission(request, self):
            return True
        return location.location_manager_id == request.user.id


class LocationListCreateView(LocationScopedView):
    """This hospital's own sites — every one of them, active or not.

    Read: any hospital staff. Write: hospital_admin only.
    """

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        queryset = self.locations().select_related("location_manager").order_by("name")

        is_active = request.query_params.get("is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() in ("true", "1", "yes"))

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(
            LocationSerializer(page, many=True, context={"request": request}).data,
        )

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        if not IsHospitalAdmin().has_permission(request, self):
            return Response(
                {"detail": "Only a hospital administrator can add a location."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = LocationSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        location = create_location(organization=org, **data)

        return Response(
            LocationSerializer(location, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class LocationDetailView(LocationScopedView):
    """One location's profile. Read: any hospital staff. Write:
    hospital_admin or this location's own manager."""

    def get(self, request, location_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        location, missing = self.get_location_or_404(location_id)
        return missing or Response(LocationSerializer(location, context={"request": request}).data)

    def patch(self, request, location_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        location, missing = self.get_location_or_404(location_id)
        if missing:
            return missing
        if not self.can_manage(request, location):
            return Response(
                {"detail": "Only a hospital administrator or this location's manager can update it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = LocationSerializer(
            location,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def delete(self, request, location_id):
        """Hard-delete — hospital_admin only, same as create/reactivate, and
        only once the location is already deactivated (see
        ``locations.services.delete_location``). Never extended to the
        location's own manager: getting rid of a site outright is a
        hospital-level call, not an operational one."""
        _, error = self.hospital_or_error(request)
        if error:
            return error
        if not IsHospitalAdmin().has_permission(request, self):
            return Response(
                {"detail": "Only a hospital administrator can delete a location."},
                status=status.HTTP_403_FORBIDDEN,
            )
        location, missing = self.get_location_or_404(location_id)
        if missing:
            return missing

        try:
            delete_location(location)
        except LocationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


class LocationAssignmentStatusView(LocationScopedView):
    """Whether this location can be deactivated right now, and why not if not —
    so the frontend never has to guess or attempt-and-fail."""

    def get(self, request, location_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        location, missing = self.get_location_or_404(location_id)
        if missing:
            return missing

        count = location.active_patient_count
        return Response(
            {
                "has_active_patients": count > 0,
                "active_patient_count": count,
                "message": (
                    f"This location can't be deactivated because {count} active patient(s) "
                    "are currently assigned to it. Move those patients to another location first."
                    if count
                    else "This location has no active patients and can be safely deactivated."
                ),
            },
        )


class LocationDeactivateView(LocationScopedView):
    """Deactivate — never delete. hospital_admin or this location's own
    manager, immediate effect, blocked only by the zero-active-patients
    guard (no approval step)."""

    def post(self, request, location_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        location, missing = self.get_location_or_404(location_id)
        if missing:
            return missing
        if not self.can_manage(request, location):
            return Response(
                {"detail": "Only a hospital administrator or this location's manager can deactivate it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            deactivate_location(location, by=request.user, reason=request.data.get("reason", ""))
        except LocationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(LocationSerializer(location, context={"request": request}).data)


class LocationReactivateView(LocationScopedView):
    """Restore a previously deactivated location. hospital_admin only —
    deliberately not extended to the (former) location manager, same as
    Neuro_RPM: resurrecting a closed site is a hospital-level call, closing
    one operationally is not."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def post(self, request, location_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        location, missing = self.get_location_or_404(location_id)
        if missing:
            return missing

        reactivate_location(location)
        return Response(LocationSerializer(location, context={"request": request}).data)


class LocationMovePatientsView(LocationScopedView):
    """Move patients out of this location — the necessary companion to
    deactivation's guard. hospital_admin or this (source) location's own
    manager — without this, a manager who can deactivate their location
    could still get stuck needing hospital_admin just to clear it first."""

    def post(self, request, location_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        source, missing = self.get_location_or_404(location_id)
        if missing:
            return missing
        if not self.can_manage(request, source):
            return Response(
                {"detail": "Only a hospital administrator or this location's manager can move its patients."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = MoveLocationPatientsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        target, missing = self.get_location_or_404(data["target_location_id"])
        if missing:
            return Response({"detail": "Target location not found."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            moved = move_patients_to_location(
                source,
                target,
                move_all=data["move_all"],
                patient_ids=data.get("patient_ids"),
            )
        except LocationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"detail": f"{moved} patient(s) moved to '{target.name}'."})


class LocationPatientsView(LocationScopedView):
    """Sub-resource: every patient at this specific location — not the whole
    hospital's roster, just this one site's. Reuses the same lean list
    serializer and prefetch the main Patient List uses, so the two shapes
    never drift apart.
    """

    def get(self, request, location_id):
        from momcare_platform.core.patients.api.serializers import PatientListSerializer  # noqa: PLC0415
        from momcare_platform.core.patients.api.views import _active_pregnancy_prefetch  # noqa: PLC0415
        from momcare_platform.core.patients.models import Patient  # noqa: PLC0415

        _, error = self.hospital_or_error(request)
        if error:
            return error
        location, missing = self.get_location_or_404(location_id)
        if missing:
            return missing

        patients = (
            Patient.objects.filter(location=location)
            .select_related("location")
            .prefetch_related(_active_pregnancy_prefetch())
            .order_by("-created_at", "id")
        )
        is_active = request.query_params.get("is_active")
        if is_active is not None:
            patients = patients.filter(is_active=is_active.lower() in ("true", "1", "yes"))

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(patients, request, view=self)
        return paginator.get_paginated_response(PatientListSerializer(page, many=True).data)
