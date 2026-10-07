"""What a patient reads of her own readings and risk -- the mobile app's side of vitals.

Strictly read-only. Readings are recorded by the hospital or a device, never by her, so
nothing here writes, and the staff endpoints (``/pregnancies/<id>/readings/`` and friends)
stay staff-only: she gets her own ``my-`` routes rather than a bent permission on theirs.

Every lookup is scoped to her hospital first and then to HER (``patient__user``), so another
woman's pregnancy -- even at the same hospital -- is "not found". Her risk comes without the
staff review workflow behind it (the model's raw answer, confidence, flagged-for-review,
review status, who confirmed it): that is how a hospital works, not something for her screen,
and the staff filters that would let her probe it are not accepted here.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsPatient
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.patients.models import Pregnancy
from momcare_platform.modules.pregnancy.vitals.api.serializers import (
    PatientRiskAssessmentSerializer,
    PatientVitalReadingSerializer,
)
from momcare_platform.modules.pregnancy.vitals.models import RiskAssessment
from momcare_platform.modules.pregnancy.vitals.services import (
    compute_vitals_summary,
    current_risk,
    latest_readings,
    readings_in_window,
    resolve_reading_period,
)

NOT_FOUND = {"detail": "Pregnancy not found."}

# The window her risk history covers unless she picks another (the same codes as the readings
# filters), and the most rows one response will carry.
DEFAULT_RISK_PERIOD = "1_month"
RISK_HISTORY_LIMIT = 100

# Worded for her, and set by code -- never left to a model.
CONTACT_MESSAGE = "Please contact your care team today."
RISK_NOTICE = (
    "This is an automatic check of your readings. It is not a diagnosis, and your care team decides "
    "what it means for your care."
)


class PatientVitalsView(OrganizationScopedQuerysetMixin, APIView):
    """Base: patients only, scoped to her hospital and then to herself."""

    permission_classes = [IsAuthenticated, IsPatient]
    organization_lookup = "patient__location__organization"

    def get_pregnancy_or_404(self, pregnancy_id):
        try:
            pregnancy = (
                self.scope_to_organization(Pregnancy.objects.filter(patient__user=self.request.user))
                .select_related("patient", "patient__location")
                .get(pk=pregnancy_id)
            )
        except Pregnancy.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        return pregnancy, None


class MyReadingsView(PatientVitalsView):
    """Her readings, newest first. ``period`` / ``start_date``+``end_date`` narrow the window and
    ``reading_type`` adds the averages, min, max and category percentages -- the same query
    parameters, and the same numbers, the hospital sees."""

    def get(self, request, pregnancy_id):
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing
        try:
            readings, statistics = readings_in_window(pregnancy, request.query_params)
        except serializers.ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except DjangoValidationError, ValueError:
            return Response({"since": ["Enter a valid date and time."]}, status=status.HTTP_400_BAD_REQUEST)

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(readings.order_by("-recorded_at", "id"), request, view=self)
        body = paginator.get_paginated_response(PatientVitalReadingSerializer(page, many=True).data)
        body.data["statistics"] = statistics
        return body


class MyLatestReadingView(PatientVitalsView):
    """Her most recent reading -- ``None``, never a made-up normal value, when there is none."""

    def get(self, request, pregnancy_id):
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing
        latest = latest_readings(pregnancy)
        return Response(
            {
                "reading": PatientVitalReadingSerializer(latest).data if latest else None,
                "total_count": pregnancy.readings.count(),
            },
        )


class MyVitalsSummaryView(PatientVitalsView):
    """Rolling 30-day average of every vital, plus this calendar month's Low/Medium/High split."""

    def get(self, request, pregnancy_id):
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing
        return Response(compute_vitals_summary(pregnancy))


class MyRiskView(PatientVitalsView):
    """Her current risk and its history, in a form fit for her screen.

    ``current`` is always the true current assessment, whatever ``period`` narrows the history
    to. A high current risk carries the contact-your-care-team message; every response carries a
    notice that this is an automatic check and not a diagnosis. The staff review filters
    (``review_status``, ``flagged_for_review``, ``actionable``) are deliberately not accepted.
    """

    def get(self, request, pregnancy_id):
        pregnancy, missing = self.get_pregnancy_or_404(pregnancy_id)
        if missing:
            return missing
        try:
            start, end = resolve_reading_period(
                request.query_params.get("period") or DEFAULT_RISK_PERIOD,
                pregnancy.patient.location.timezone,
            )
        except serializers.ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)

        current = current_risk(pregnancy)
        history = (
            pregnancy.risk_assessments.select_related("reading")
            .filter(assessed_at__gte=start, assessed_at__lte=end)
            .order_by("-assessed_at", "id")[:RISK_HISTORY_LIMIT]
        )
        contact = current is not None and current.final_risk_level == RiskAssessment.LEVEL_HIGH
        return Response(
            {
                "current": PatientRiskAssessmentSerializer(current).data if current else None,
                "history": PatientRiskAssessmentSerializer(history, many=True).data,
                "contact_care_team": contact,
                "contact_message": CONTACT_MESSAGE if contact else None,
                "notice": RISK_NOTICE,
            },
        )
