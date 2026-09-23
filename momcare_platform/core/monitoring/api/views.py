"""Clinical contact logging endpoints -- monitoring sessions, notes, and the
tag catalogue they draw from.

Sessions/notes hang off a Patient (see models.py's own docstring for why,
not Pregnancy alone), reached through the caller's own hospital so another
tenant's patient can never be found this way. ClinicalTag hangs off either
an Organization or one of its Locations -- see ``visible_clinical_tags``.
"""

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsHospitalStaff, user_role_code
from momcare_platform.core.common.scoping import (
    OrganizationScopedQuerysetMixin,
    sees_all_locations_in_org,
    user_location_ids,
)
from momcare_platform.core.monitoring.api.permissions import IsOwnerOrHospitalAdmin
from momcare_platform.core.monitoring.api.serializers import (
    ClinicalTagSerializer,
    CombinedMonitoringSerializer,
    MonitoringNoteSerializer,
    MonitoringSessionSerializer,
)
from momcare_platform.core.monitoring.models import ClinicalTag, MonitoringNote, MonitoringSession
from momcare_platform.core.monitoring.services import (
    create_combined_monitoring,
    monitoring_period_totals,
    month_bounds,
)
from momcare_platform.core.patients.models import Patient

NO_HOSPITAL = {"detail": "This account is not attached to a hospital."}


def visible_clinical_tags(request, org):
    """Tags this caller may see: their hospital's org-level tags, plus
    whichever locations they can see into -- every location for an admin,
    only their own assigned ones for a location-scoped role. Shared shape
    with ``services._tag_scope``, but that one is single-location (a note
    is filed at exactly one location); this one spans however many
    locations the caller themself can see.
    """
    qs = ClinicalTag.objects.filter(organization=org, location__isnull=True)
    if sees_all_locations_in_org(request.user):
        qs = qs | ClinicalTag.objects.filter(location__organization=org)
    else:
        qs = qs | ClinicalTag.objects.filter(location_id__in=user_location_ids(request.user))
    return qs.distinct()


class MonitoringView(OrganizationScopedQuerysetMixin, APIView):
    """Base for patient-scoped monitoring endpoints."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "organization"

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def get_patient_or_404(self, patient_id):
        """Scope first, then look up, so another hospital's patient
        resolves to nothing rather than being found and refused."""
        try:
            patient = self.scope_to_organization(Patient.objects.select_related("location")).get(pk=patient_id)
        except Patient.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        return patient, None

    def resolve_month_range(self, request, patient):
        """``?year=&month=`` -- defaults to the current month in the
        patient's location timezone, same as Neuro_RPM's own
        month-totals endpoints. Returns ``(start, end, year, month, None)``
        on success, or ``(None, None, None, None, error_response)``.
        """
        now_local = timezone.localtime(timezone.now(), timezone=patient.location.timezone)
        try:
            year = int(request.query_params.get("year", now_local.year))
            month = int(request.query_params.get("month", now_local.month))
        except ValueError:
            error = Response({"detail": "year and month must be integers."}, status=status.HTTP_400_BAD_REQUEST)
            return None, None, None, None, error
        if not 1 <= month <= 12:
            error = Response({"detail": "month must be between 1 and 12."}, status=status.HTTP_400_BAD_REQUEST)
            return None, None, None, None, error
        start, end = month_bounds(year=year, month=month, tzinfo=patient.location.timezone)
        return start, end, year, month, None


class PatientMonitoringView(MonitoringView):
    """A patient's monitoring feed: sessions and notes merged into one
    chronological timeline, and creating a new contact (session, note, or
    both) atomically.
    """

    def _combined_entries(self, patient, *, start=None, end=None):
        sessions = patient.monitoring_sessions.select_related("added_by", "pregnancy").prefetch_related("note__tags")
        standalone_notes = (
            patient.monitoring_notes.filter(session__isnull=True)
            .select_related("added_by", "pregnancy")
            .prefetch_related("tags")
        )
        if start is not None and end is not None:
            sessions = sessions.filter(recorded_at__gte=start, recorded_at__lte=end)
            standalone_notes = standalone_notes.filter(recorded_at__gte=start, recorded_at__lte=end)

        entries = []
        for session in sessions:
            note = getattr(session, "note", None)
            entries.append(
                {
                    "recorded_at": session.recorded_at,
                    "session": MonitoringSessionSerializer(session).data,
                    "note": MonitoringNoteSerializer(note).data if note else None,
                },
            )
        for note in standalone_notes:
            entries.append(
                {
                    "recorded_at": note.recorded_at,
                    "session": None,
                    "note": MonitoringNoteSerializer(note).data,
                },
            )
        entries.sort(key=lambda entry: entry["recorded_at"], reverse=True)
        return entries

    def get(self, request, patient_id):
        """Scoped to one calendar month -- defaults to the current one in
        the patient's location timezone, ``?year=&month=`` to browse
        another -- with a ``totals`` block for that same month. Same shape
        as Neuro_RPM's combined endpoint, minus the per-program split
        MomCare has no use for.
        """
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        start, end, year, month, error = self.resolve_month_range(request, patient)
        if error:
            return error

        entries = self._combined_entries(patient, start=start, end=end)
        totals = monitoring_period_totals(patient=patient, start=start, end=end)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(entries, request, view=self)
        body = paginator.get_paginated_response(page)
        body.data["totals"] = totals
        body.data["year"] = year
        body.data["month"] = month
        return body

    def post(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        serializer = CombinedMonitoringSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        session, note = create_combined_monitoring(
            patient=patient,
            duration_seconds=data.get("duration_seconds"),
            recorded_at=data.get("recorded_at") or timezone.now(),
            added_by=request.user,
            note_text=data.get("note", ""),
            tags=data.get("tags", []),
            left_voicemail=data.get("left_voicemail", False),
            two_way_communication=data.get("two_way_communication", False),
        )
        payload = {
            "session": MonitoringSessionSerializer(session).data if session else None,
            "note": MonitoringNoteSerializer(note).data if note else None,
        }
        return Response(payload, status=status.HTTP_201_CREATED)


class PatientMonitoringSessionsView(MonitoringView):
    """This patient's sessions for one calendar month, with a ``totals``
    block -- "how much time has been logged". Defaults to the current
    month in the patient's location timezone; ``?year=&month=`` browses
    another."""

    def get(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        start, end, year, month, error = self.resolve_month_range(request, patient)
        if error:
            return error

        sessions = (
            patient.monitoring_sessions.select_related("added_by", "pregnancy")
            .filter(recorded_at__gte=start, recorded_at__lte=end)
            .order_by("-recorded_at", "id")
        )
        totals = monitoring_period_totals(patient=patient, start=start, end=end)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(sessions, request, view=self)
        body = paginator.get_paginated_response(MonitoringSessionSerializer(page, many=True).data)
        body.data["totals"] = totals
        body.data["year"] = year
        body.data["month"] = month
        return body


class PatientMonitoringNotesView(MonitoringView):
    """This patient's notes only, with free-text and tag filtering --
    used to search a patient's clinical contact history."""

    def get(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        notes = (
            patient.monitoring_notes.select_related("added_by", "pregnancy", "session")
            .prefetch_related("tags")
            .order_by("-recorded_at", "id")
        )
        search = request.query_params.get("search")
        if search:
            notes = notes.filter(
                Q(note__icontains=search)
                | Q(tags__name__icontains=search)
                | Q(added_by__first_name__icontains=search)
                | Q(added_by__last_name__icontains=search),
            ).distinct()
        tag_id = request.query_params.get("tag_id")
        if tag_id:
            notes = notes.filter(tags__id=tag_id)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(notes, request, view=self)
        return paginator.get_paginated_response(MonitoringNoteSerializer(page, many=True).data)


class MonitoringSessionDetailView(OrganizationScopedQuerysetMixin, APIView):
    """A single session by id -- flat, not nested under its patient, the
    same way ``/alerts/<id>/`` is flat even though an Alert hangs off a
    Pregnancy."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "patient__organization"

    def get_session_or_404(self, session_id):
        try:
            session = self.scope_to_organization(
                MonitoringSession.objects.select_related("patient", "pregnancy", "added_by"),
            ).get(pk=session_id)
        except MonitoringSession.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Monitoring session not found."}, status=status.HTTP_404_NOT_FOUND)
        return session, None

    def get(self, request, session_id):
        session, missing = self.get_session_or_404(session_id)
        if missing:
            return missing
        return Response(MonitoringSessionSerializer(session).data)

    def _update(self, request, session_id, *, partial):
        session, missing = self.get_session_or_404(session_id)
        if missing:
            return missing
        if not IsOwnerOrHospitalAdmin().has_object_permission(request, self, session):
            return Response(
                {"detail": "Only the staff member who logged this session, or a hospital admin, can edit it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = MonitoringSessionSerializer(session, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def put(self, request, session_id):
        return self._update(request, session_id, partial=False)

    def patch(self, request, session_id):
        return self._update(request, session_id, partial=True)

    def delete(self, request, session_id):
        session, missing = self.get_session_or_404(session_id)
        if missing:
            return missing
        if not IsOwnerOrHospitalAdmin().has_object_permission(request, self, session):
            return Response(
                {"detail": "Only the staff member who logged this session, or a hospital admin, can delete it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        session.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MonitoringNoteDetailView(OrganizationScopedQuerysetMixin, APIView):
    """A single note by id -- flat, same reasoning as sessions above."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "patient__organization"

    def get_note_or_404(self, note_id):
        try:
            note = self.scope_to_organization(
                MonitoringNote.objects.select_related("patient", "pregnancy", "added_by", "session").prefetch_related(
                    "tags",
                ),
            ).get(pk=note_id)
        except MonitoringNote.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Monitoring note not found."}, status=status.HTTP_404_NOT_FOUND)
        return note, None

    def get(self, request, note_id):
        note, missing = self.get_note_or_404(note_id)
        if missing:
            return missing
        return Response(MonitoringNoteSerializer(note).data)

    def _update(self, request, note_id, *, partial):
        note, missing = self.get_note_or_404(note_id)
        if missing:
            return missing
        if not IsOwnerOrHospitalAdmin().has_object_permission(request, self, note):
            return Response(
                {"detail": "Only the staff member who wrote this note, or a hospital admin, can edit it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = MonitoringNoteSerializer(note, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    def put(self, request, note_id):
        return self._update(request, note_id, partial=False)

    def patch(self, request, note_id):
        return self._update(request, note_id, partial=True)

    def delete(self, request, note_id):
        note, missing = self.get_note_or_404(note_id)
        if missing:
            return missing
        if not IsOwnerOrHospitalAdmin().has_object_permission(request, self, note):
            return Response(
                {"detail": "Only the staff member who wrote this note, or a hospital admin, can delete it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        note.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ClinicalTagListCreateView(APIView):
    """List: any hospital-side role (powers the tag picker). Create: hospital
    admins only -- a tag, even a location-scoped one, is shared configuration
    used across every note that references it, so adding one is a
    deliberate admin action, not a per-note side effect (ad-hoc tags typed
    inline during note creation still get auto-created -- see
    ``services.get_or_create_tags`` -- this endpoint is for curating the
    catalogue directly).
    """

    permission_classes = [IsAuthenticated, IsHospitalStaff]

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def get(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        tags = visible_clinical_tags(request, org).order_by("name")
        location_id = request.query_params.get("location_id")
        if location_id:
            tags = tags.filter(location_id=location_id)
        serializer = ClinicalTagSerializer(tags, many=True)
        return Response({"count": len(serializer.data), "results": serializer.data})

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        if not (request.user.is_superuser or user_role_code(request.user) == settings.ROLE_HOSPITAL_ADMIN):
            return Response(
                {"detail": "Only a hospital admin can manage the tag catalogue."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = ClinicalTagSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            # The uniqueness constraints are conditional (organization+name
            # OR location+name), which DRF's ModelSerializer does not
            # auto-validate -- caught here rather than left to surface as a
            # raw 500. A savepoint (not the bare call) so a caught failure
            # doesn't poison the rest of this request's transaction.
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return Response(
                {"name": ["A tag with this name already exists in this scope."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class ClinicalTagDetailView(APIView):
    """Read: any hospital-side role, within what they can see (see
    ``visible_clinical_tags``). Edit/delete: hospital admins only."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def get_tag_or_404(self, request, org, tag_id):
        try:
            tag = visible_clinical_tags(request, org).get(pk=tag_id)
        except ClinicalTag.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Clinical tag not found."}, status=status.HTTP_404_NOT_FOUND)
        return tag, None

    def get(self, request, tag_id):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        tag, missing = self.get_tag_or_404(request, org, tag_id)
        if missing:
            return missing
        return Response(ClinicalTagSerializer(tag).data)

    def _admin_or_403(self, request):
        if request.user.is_superuser or user_role_code(request.user) == settings.ROLE_HOSPITAL_ADMIN:
            return None
        return Response(
            {"detail": "Only a hospital admin can manage the tag catalogue."},
            status=status.HTTP_403_FORBIDDEN,
        )

    def _update(self, request, tag_id, *, partial):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        forbidden = self._admin_or_403(request)
        if forbidden:
            return forbidden
        tag, missing = self.get_tag_or_404(request, org, tag_id)
        if missing:
            return missing
        serializer = ClinicalTagSerializer(tag, data=request.data, partial=partial, context={"request": request})
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
        except IntegrityError:
            return Response(
                {"name": ["A tag with this name already exists in this scope."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(serializer.data)

    def put(self, request, tag_id):
        return self._update(request, tag_id, partial=False)

    def patch(self, request, tag_id):
        return self._update(request, tag_id, partial=True)

    def delete(self, request, tag_id):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        forbidden = self._admin_or_403(request)
        if forbidden:
            return forbidden
        tag, missing = self.get_tag_or_404(request, org, tag_id)
        if missing:
            return missing
        tag.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
