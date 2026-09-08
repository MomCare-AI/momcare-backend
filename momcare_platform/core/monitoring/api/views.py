"""Monitoring endpoints — readings and devices.

Everything here hangs off a pregnancy that is resolved through the caller's own
hospital, so a reading can never be filed against another tenant's patient.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsClinician, IsHospitalStaff
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.monitoring.api.serializers import (
    AttentionPatientSerializer,
    DeviceAssignSerializer,
    DeviceSerializer,
    RiskAssessmentSerializer,
    VitalReadingCreateSerializer,
    VitalReadingSerializer,
)
from momcare_platform.core.monitoring.models import Device, RiskAssessment, VitalReading
from momcare_platform.core.monitoring.services import (
    MonitoringError,
    assign_device,
    current_risk,
    latest_readings,
    reassess_risk,
    unassign_device,
)
from momcare_platform.core.patients.models import Pregnancy

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
        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        readings = pregnancy.readings.all()

        since = request.query_params.get("since")
        if since:
            readings = readings.filter(recorded_at__gte=since)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(readings.order_by("-recorded_at", "id"), request, view=self)
        return paginator.get_paginated_response(VitalReadingSerializer(page, many=True).data)

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

        # Score immediately, so a dangerous reading is judged as it arrives
        # rather than whenever a scheduler next runs.
        assessment = reassess_risk(pregnancy)

        body = VitalReadingSerializer(reading).data
        body["risk_changed"] = assessment is not None
        body["risk_level"] = assessment.final_risk_level if assessment else None
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


class VerifyRiskView(MonitoringView):
    """A clinician's review of one risk assessment: confirm the model's
    answer, or correct it.

    There is no "just seen, not confirmed" state — a ``confirmed_risk_level``
    is required. An assessment nobody has agreed with or corrected has not
    actually been reviewed, whatever a bare timestamp might imply.
    ``review_status`` is derived here, not chosen by the caller: confirmed
    when it matches ``final_risk_level``, corrected when it does not.

    Clinicians only, which this docstring always claimed and the permissions
    did not enforce. A hospital administrator is not required to have any
    clinical training, and an assessment marked reviewed by somebody who could
    not review it is worse than one left unreviewed — the queue would look
    attended to.
    """

    permission_classes = [IsAuthenticated, IsClinician]

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

        assessment.confirmed_risk_level = confirmed
        assessment.review_status = (
            RiskAssessment.REVIEW_CONFIRMED
            if confirmed == assessment.final_risk_level
            else RiskAssessment.REVIEW_CORRECTED
        )
        assessment.verified_at = timezone.now()
        assessment.verified_by = request.user
        assessment.save(
            update_fields=["confirmed_risk_level", "review_status", "verified_at", "verified_by"],
        )

        return Response(RiskAssessmentSerializer(assessment).data)


class AttentionQueueView(MonitoringView):
    """Patients whose latest assessment is not stable — the working list.

    Ordered by severity, then by how long the assessment has gone
    unreviewed, so the most urgent unreviewed case is always at the top.
    """

    organization_lookup = "patient__location__organization"

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        pregnancies = (
            self.scope_to_organization(Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE))
            .select_related("patient", "assigned_staff__user")
            .prefetch_related("risk_assessments")
        )

        rows = []
        for pregnancy in pregnancies:
            current = pregnancy.risk_assessments.first()
            if current is None or not current.is_actionable:
                continue
            rows.append(
                {
                    "patient_id": pregnancy.patient_id,
                    "pregnancy_id": pregnancy.id,
                    "full_name": pregnancy.patient.full_name,
                    "mrn": pregnancy.patient.mrn,
                    "gestational_age": pregnancy.gestational_age_display,
                    # final_risk_level: the level actually acted on.
                    "risk_level": current.final_risk_level,
                    "risk_level_display": current.get_final_risk_level_display(),
                    "assessed_at": current.assessed_at,
                    "needs_review": current.needs_review,
                    "assigned_staff_name": (
                        pregnancy.assigned_staff.user.get_full_name() if pregnancy.assigned_staff_id else ""
                    ),
                    "has_responsible_clinician": pregnancy.has_responsible_clinician,
                },
            )

        # Same canonical encoding as the trained model everywhere in this
        # project: Low=0, Medium=1, High=2. Negated only here, because a
        # queue needs the *worst* case first, and that means sorting on the
        # opposite of what the number itself means.
        severity = {"low": 0, "medium": 1, "high": 2}
        rows.sort(
            key=lambda r: (
                -severity.get(r["risk_level"], 0),
                not r["needs_review"],
                r["assessed_at"],
            ),
        )

        return Response(
            {
                "count": len(rows),
                "results": AttentionPatientSerializer(rows, many=True).data,
            },
        )


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
        return Response(DeviceSerializer(devices, many=True).data)

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
