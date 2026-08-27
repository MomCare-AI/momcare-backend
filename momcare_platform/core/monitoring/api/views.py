"""Monitoring endpoints — readings, devices, and the simulator.

Everything here hangs off a pregnancy that is resolved through the caller's own
hospital, so a reading can never be filed against another tenant's patient.
"""

from django.conf import settings
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
    SimulateSerializer,
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
    simulate_readings,
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
        except (Pregnancy.DoesNotExist, DjangoValidationError, ValueError):
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

        reading_type = request.query_params.get("type")
        if reading_type:
            readings = readings.filter(reading_type=reading_type)

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
            reading_type=data["reading_type"],
            value=data["value"],
            value_secondary=data.get("value_secondary"),
            recorded_at=data.get("recorded_at") or timezone.now(),
            source=data["source"],
            device=pregnancy.devices.filter(status=Device.STATUS_ASSIGNED).first(),
            recorded_by=request.user if data["source"] == VitalReading.SOURCE_MANUAL else None,
        )

        # Score immediately, so a dangerous reading is judged as it arrives
        # rather than whenever a scheduler next runs.
        assessment = reassess_risk(pregnancy)

        body = VitalReadingSerializer(reading).data
        body["risk_changed"] = assessment is not None
        body["risk_level"] = assessment.level if assessment else None
        return Response(body, status=status.HTTP_201_CREATED)


class LatestReadingsView(MonitoringView):
    """The most recent reading of each type, for the patient header.

    A type with no readings is simply absent rather than reported as normal —
    a screen that looks calm because data stopped arriving is the worst failure
    a monitoring system can have.
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
                "readings": {
                    reading_type: VitalReadingSerializer(reading).data
                    for reading_type, reading in latest.items()
                },
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

        assessments = pregnancy.risk_assessments.select_related("acknowledged_by")[:50]
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


class AcknowledgeRiskView(MonitoringView):
    """Record that a clinician has seen a risk assessment.

    Acknowledgement is what turns a flag into accountability: it names who
    looked and when, so an unreviewed assessment stays visibly unreviewed
    rather than fading from the queue on its own.

    Clinicians only, which this docstring always claimed and the permissions did
    not enforce. A hospital administrator is not required to have any clinical
    training, and an assessment marked reviewed by somebody who could not review
    it is worse than one left unreviewed — the queue would look attended to.
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
        except (RiskAssessment.DoesNotExist, DjangoValidationError, ValueError):
            return Response({"detail": "Assessment not found."}, status=status.HTTP_404_NOT_FOUND)

        if assessment.acknowledged_at is None:
            assessment.acknowledged_at = timezone.now()
            assessment.acknowledged_by = request.user
            assessment.save(update_fields=["acknowledged_at", "acknowledged_by"])

        return Response(RiskAssessmentSerializer(assessment).data)


class AttentionQueueView(MonitoringView):
    """Patients whose latest assessment is not stable — the working list.

    Ordered by severity, then by how long the assessment has gone
    unacknowledged, so the most urgent unreviewed case is always at the top.
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
                    "level": current.level,
                    "level_display": current.get_level_display(),
                    "reasons": current.reasons,
                    "assessed_at": current.assessed_at,
                    "needs_acknowledgement": current.needs_acknowledgement,
                    "assigned_staff_name": (
                        pregnancy.assigned_staff.user.get_full_name()
                        if pregnancy.assigned_staff_id
                        else ""
                    ),
                    "has_responsible_clinician": pregnancy.has_responsible_clinician,
                },
            )

        severity = {"critical": 0, "high": 1, "moderate": 2}
        rows.sort(
            key=lambda r: (
                severity.get(r["level"], 9),
                not r["needs_acknowledgement"],
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

        device = (
            Device.objects.filter(
                organization=request.user.organization,
                pk=serializer.validated_data["device_id"],
            )
            .first()
        )
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


class SimulateReadingsView(MonitoringView):
    """Generate readings so the pipeline can be exercised before hardware exists.

    Refused when DEBUG is off: simulated observations must never be creatable
    on a deployment that also holds real ones.
    """

    def post(self, request, pregnancy_id):
        if not settings.DEBUG:
            return Response(
                {"detail": "Simulated readings are only available in development."},
                status=status.HTTP_403_FORBIDDEN,
            )

        _, error = self.hospital_or_error(request)
        if error:
            return error
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing

        serializer = SimulateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            readings = simulate_readings(
                pregnancy=pregnancy,
                hours=serializer.validated_data["hours"],
                elevated=serializer.validated_data["elevated"],
                device=pregnancy.devices.filter(status=Device.STATUS_ASSIGNED).first(),
            )
        except MonitoringError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "detail": f"Generated {len(readings)} simulated readings.",
                "created": len(readings),
                "simulated": True,
            },
            status=status.HTTP_201_CREATED,
        )
