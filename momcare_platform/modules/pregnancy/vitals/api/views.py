"""Monitoring endpoints — readings and devices.

Everything here hangs off a pregnancy that is resolved through the caller's own
hospital, so a reading can never be filed against another tenant's patient.
"""

from collections.abc import Callable

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsClinician, IsHospitalStaff
from momcare_platform.core.common.scoping import (
    OrganizationScopedQuerysetMixin,
    scope_to_assigned_staff,
)
from momcare_platform.core.patients.models import Pregnancy
from momcare_platform.modules.pregnancy.vitals.api.serializers import (
    DeviceAssignSerializer,
    DeviceSerializer,
    RiskAssessmentSerializer,
    VitalReadingCreateSerializer,
    VitalReadingSerializer,
)
from momcare_platform.modules.pregnancy.vitals.models import Device, RiskAssessment, VitalReading
from momcare_platform.modules.pregnancy.vitals.services import (
    MonitoringError,
    assign_device,
    bulk_resolve_risk_reviews,
    compute_reading_statistics,
    compute_vitals_summary,
    current_risk,
    escalate_risk,
    latest_readings,
    reassess_risk,
    resolve_custom_reading_range,
    resolve_reading_period,
    review_risk,
    unassign_device,
)

NO_HOSPITAL = {"detail": "This account is not attached to a hospital."}


class MonitoringView(OrganizationScopedQuerysetMixin, APIView):
    """Base for monitoring endpoints, scoped to the caller's hospital."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "patient__location__organization"

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def get_pregnancy_or_404(self, pregnancy_id):
        """Scope first, then look up, so another hospital's pregnancy resolves
        to nothing rather than being found and refused."""
        try:
            pregnancy = (
                self.scope_to_organization(Pregnancy.objects.all())
                .select_related("patient", "patient__location")
                .get(pk=pregnancy_id)
            )
        except Pregnancy.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response(
                {"detail": "Pregnancy not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return pregnancy, None


class ReadingListCreateView(MonitoringView):
    """A pregnancy's readings, and recording a new one."""

    def get(self, request, pregnancy_id):
        """``period``/``start_date``+``end_date`` narrow the window (a custom
        range wins if both are given); ``reading_type`` is what actually
        triggers the ``statistics`` block -- no separate flag, matching
        Neuro_RPM's own trigger. See
        docs/design/2026-09-25-reading-statistics-design.md.
        """
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        readings = pregnancy.readings.all()
        tzinfo = pregnancy.patient.location.timezone

        since = request.query_params.get("since")
        if since:
            readings = readings.filter(recorded_at__gte=since)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        period = request.query_params.get("period")
        try:
            if start_date or end_date:
                start, end = resolve_custom_reading_range(start_date, end_date, tzinfo)
                readings = readings.filter(recorded_at__gte=start, recorded_at__lte=end)
            elif period:
                start, end = resolve_reading_period(period, tzinfo)
                readings = readings.filter(recorded_at__gte=start, recorded_at__lte=end)
        except serializers.ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

        statistics: dict = {}
        reading_type = request.query_params.get("reading_type")
        if reading_type:
            try:
                statistics = compute_reading_statistics(readings, reading_type)
            except serializers.ValidationError as exc:
                return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(readings.order_by("-recorded_at", "id"), request, view=self)
        body = paginator.get_paginated_response(VitalReadingSerializer(page, many=True).data)
        body.data["statistics"] = statistics
        return body

    def post(self, request, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        if not pregnancy.is_active:
            return Response(
                {"detail": "Readings can only be recorded against an active pregnancy."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = VitalReadingCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Captured before the write so the response can still say whether
        # risk changed, without this view being the one thing responsible
        # for triggering the check — see vitals/signals.py: creating the
        # reading below fires a post_save signal that scores it
        # automatically, inside this same transaction, for any code path
        # that creates a VitalReading, not just this one.
        previous_assessment = current_risk(pregnancy)

        reading = VitalReading.objects.create(
            pregnancy=pregnancy,
            age=data.get("age"),
            systolic_bp=data.get("systolic_bp"),
            diastolic_bp=data.get("diastolic_bp"),
            heart_rate=data.get("heart_rate"),
            body_temp_f=data.get("body_temp_f"),
            hemoglobin=data.get("hemoglobin"),
            blood_glucose=data.get("blood_glucose"),
            stress_score=data.get("stress_score"),
            phys_activity_score=data.get("phys_activity_score"),
            source=data["source"],
            recorded_at=data.get("recorded_at") or timezone.now(),
            device=pregnancy.devices.filter(status=Device.STATUS_ASSIGNED).first(),
            recorded_by=request.user,
        )

        current_assessment = current_risk(pregnancy)
        if current_assessment is not None and (
            previous_assessment is None or current_assessment.id != previous_assessment.id
        ):
            risk_changed, risk_level = True, current_assessment.final_risk_level
        else:
            risk_changed, risk_level = False, None

        body = VitalReadingSerializer(reading).data
        body["risk_changed"] = risk_changed
        body["risk_level"] = risk_level
        return Response(body, status=status.HTTP_201_CREATED)


class LatestReadingsView(MonitoringView):
    """The most recent reading event, for the patient header.

    None when there isn't one yet, rather than a fabricated normal-looking
    value — a screen that looks calm because data stopped arriving is the
    worst failure a monitoring system can have.
    """

    def get(self, request, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        latest = latest_readings(pregnancy)
        return Response(
            {
                "reading": VitalReadingSerializer(latest).data if latest else None,
                "total_count": pregnancy.readings.count(),
            },
        )


class VitalsSummaryView(MonitoringView):
    """Rolling 30-day average across every vital, no filters -- the quick
    glance-at-it card. See docs/design/2026-09-25-reading-statistics-design.md.
    """

    def get(self, request, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        return Response(compute_vitals_summary(pregnancy))


class RiskAssessmentView(MonitoringView):
    """A pregnancy's risk history — a record of transitions, not of readings."""

    def get(self, request, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        assessments = pregnancy.risk_assessments.select_related("verified_by")[:50]
        current = assessments[0] if assessments else None

        return Response(
            {
                "current": RiskAssessmentSerializer(current).data if current else None,
                "history": RiskAssessmentSerializer(assessments, many=True).data,
            },
        )

    def post(self, request, pregnancy_id):
        """Re-run scoring on demand — useful after correcting a reading."""
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        assessment = reassess_risk(pregnancy)
        if assessment is None:
            current = current_risk(pregnancy)
            return Response(
                {
                    "detail": "No change in risk level.",
                    "current": RiskAssessmentSerializer(current).data if current else None,
                },
            )
        return Response(RiskAssessmentSerializer(assessment).data, status=status.HTTP_201_CREATED)


class _RiskReviewActionView(MonitoringView):
    """Shared body for ``ReviewRiskView``/``EscalateRiskView`` — same lookup
    and ``confirmed_risk_level`` validation, differing only in which
    ``review_status`` the resolver lands on. Split into two endpoints (rather
    than one endpoint deriving the status) to match Neuro_RPM's own
    ``review()``/``escalate()`` action split exactly — the clinician picks
    which one applies, it isn't inferred from whether they agreed with the
    model.

    Clinicians only. A hospital administrator is not required to have any
    clinical training, and an assessment marked reviewed by somebody who
    could not review it is worse than one left pending — the queue would
    look attended to.
    """

    permission_classes = [IsAuthenticated, IsClinician]
    resolver: Callable[..., RiskAssessment]

    def post(self, request, pregnancy_id, assessment_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        try:
            assessment = pregnancy.risk_assessments.get(pk=assessment_id)
        except RiskAssessment.DoesNotExist, DjangoValidationError, ValueError:
            return Response({"detail": "Assessment not found."}, status=status.HTTP_404_NOT_FOUND)

        confirmed = request.data.get("confirmed_risk_level")
        valid_levels = {choice[0] for choice in RiskAssessment.LEVEL_CHOICES}
        if confirmed not in valid_levels:
            return Response(
                {"detail": f"confirmed_risk_level must be one of {sorted(valid_levels)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            assessment = self.resolver(assessment, confirmed_risk_level=confirmed, actor=request.user)
        except MonitoringError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(RiskAssessmentSerializer(assessment).data)


class ReviewRiskView(_RiskReviewActionView):
    """Mark a pending assessment as reviewed — no further action needed."""

    resolver = staticmethod(review_risk)


class EscalateRiskView(_RiskReviewActionView):
    """Mark a pending assessment as escalated — a triage label only. Matches
    Neuro_RPM's own ``escalate()`` exactly: no Alert side effect, no
    notification. MomCare's real escalation ladder (``Alert``/``AlertEvent``)
    already runs independently of this field — see ``escalate_risk()``'s
    docstring."""

    resolver = staticmethod(escalate_risk)


class AttentionQueueView(MonitoringView):
    """Patients whose current pregnancy has at least one flagged, still-
    pending assessment — "whose vitals just crossed a threshold and nobody
    has looked yet." Adapted from Neuro_RPM's Reading Review Workflow
    (``?workflow=reading_review`` on their Patient list + their
    ``dashboard-kpis`` count) — one patient-centric list rather than a query
    param on the Patient endpoint, since that's this project's own
    convention for a standing queue (see ``AlertListView``, which the
    scoping/pagination shape below mirrors exactly).

    The ``count`` in the pagination envelope doubles as the KPI number —
    same convention ``AlertListView`` already uses for its own
    ``unacknowledged`` badge, so no separate counts-only endpoint is needed.
    """

    organization_lookup = "patient__location__organization"

    def _scope_to_assigned(self, queryset, request):
        return scope_to_assigned_staff(queryset, request, path_prefix="")

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        latest_flagged = RiskAssessment.objects.filter(
            pregnancy=OuterRef("pk"),
            flagged_for_review=True,
            review_status=RiskAssessment.REVIEW_PENDING,
        ).order_by("-assessed_at")

        queryset = (
            self.scope_to_organization(Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE))
            .filter(
                risk_assessments__flagged_for_review=True,
                risk_assessments__review_status=RiskAssessment.REVIEW_PENDING,
            )
            .annotate(flagged_assessment_id=Subquery(latest_flagged.values("id")[:1]))
            .select_related("patient")
            .distinct()
            .order_by("-created_at")
        )
        queryset = self._scope_to_assigned(queryset, request)

        paginator = DefaultPagination()
        page = list(paginator.paginate_queryset(queryset, request, view=self))

        assessments = {
            assessment.id: assessment
            for assessment in RiskAssessment.objects.filter(
                id__in=[pregnancy.flagged_assessment_id for pregnancy in page],
            ).select_related("reading")
        }
        results = [
            {
                "patient_id": pregnancy.patient_id,
                "patient_name": pregnancy.patient.full_name,
                "pregnancy_id": pregnancy.id,
                "assessment": RiskAssessmentSerializer(assessments[pregnancy.flagged_assessment_id]).data,
            }
            for pregnancy in page
        ]
        return paginator.get_paginated_response(results)


class RiskBulkReviewView(MonitoringView):
    """Review or escalate many pending assessments in one call — ported from
    Neuro_RPM's ``bulk-review`` action. Body: ``{"items": [{"assessment_id":
    <uuid>, "review_status": "reviewed" | "escalated", "confirmed_risk_level":
    "low" | "medium" | "high"}, ...]}``.
    """

    permission_classes = [IsAuthenticated, IsClinician]
    organization_lookup = "pregnancy__patient__location__organization"

    def post(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        raw_items = request.data.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return Response({"detail": "'items' must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

        status_targets = {"reviewed": RiskAssessment.REVIEW_REVIEWED, "escalated": RiskAssessment.REVIEW_ESCALATED}
        valid_levels = {choice[0] for choice in RiskAssessment.LEVEL_CHOICES}
        parsed_items = []
        for index, entry in enumerate(raw_items):
            if not isinstance(entry, dict) or "assessment_id" not in entry or "review_status" not in entry:
                return Response(
                    {"detail": f"Item {index}: must be an object with 'assessment_id' and 'review_status'."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            target = status_targets.get(str(entry.get("review_status")))
            if target is None:
                return Response(
                    {"detail": f"Item {index}: review_status must be one of {sorted(status_targets)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            confirmed = entry.get("confirmed_risk_level")
            if confirmed not in valid_levels:
                return Response(
                    {"detail": f"Item {index}: confirmed_risk_level must be one of {sorted(valid_levels)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            parsed_items.append((str(entry["assessment_id"]), target, confirmed))

        queryset = self.scope_to_organization(RiskAssessment.objects.all())
        try:
            updated = bulk_resolve_risk_reviews(parsed_items, actor=request.user, queryset=queryset)
        except MonitoringError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(RiskAssessmentSerializer(updated, many=True).data)


class DeviceListCreateView(MonitoringView):
    """The hospital's devices. Registering stock is an admin task."""

    organization_lookup = "organization"

    def get(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        devices = (
            self.scope_to_organization(Device.objects.all())
            .select_related("assigned_pregnancy__patient")
            .order_by("serial_number")
        )
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(devices, request, view=self)
        return paginator.get_paginated_response(DeviceSerializer(page, many=True).data)

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        serializer = DeviceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        device = Device.objects.create(organization=org, **serializer.validated_data)
        return Response(DeviceSerializer(device).data, status=status.HTTP_201_CREATED)


class DeviceAssignView(MonitoringView):
    """Put a band on a wrist, or take it off."""

    def post(self, request, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        serializer = DeviceAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device = Device.objects.filter(
            organization=request.user.organization,
            pk=serializer.validated_data["device_id"],
        ).first()
        if device is None:
            return Response({"detail": "Device not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            assign_device(
                device=device,
                pregnancy=pregnancy,
                acquisition=serializer.validated_data.get("acquisition", ""),
            )
        except MonitoringError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(DeviceSerializer(device).data)

    def delete(self, request, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        device = pregnancy.devices.filter(status=Device.STATUS_ASSIGNED).first()
        if device is None:
            return Response(
                {"detail": "This patient is not wearing a device."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        unassign_device(device=device)
        return Response(DeviceSerializer(device).data)
