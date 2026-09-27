from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai.api.serializers import AISummarySerializer
from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.common.permissions import IsHospitalStaff
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.patients.models import Patient


class PatientAISummaryView(OrganizationScopedQuerysetMixin, APIView):
    """Read the cached AI Summary for one patient. Never generates one on
    the fly -- see the design doc's Triggers section for the only paths
    that ever call generate_patient_summary().

    Organization-scoped, not location-scoped, matching PatientDetailView
    (core.patients.api.views.PatientScopedView) -- a location-scoped role
    with no location assigned, or assigned to a different location than
    this patient's, could otherwise read the patient's own detail page but
    get a 404 on her AI summary.
    """

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "location__organization"

    def get(self, request, patient_id):
        patients = self.scope_to_organization(Patient.objects.all())
        patient = get_object_or_404(patients, pk=patient_id)
        summary = AISummary.objects.filter(patient=patient).first()
        if summary is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(AISummarySerializer(summary).data)
