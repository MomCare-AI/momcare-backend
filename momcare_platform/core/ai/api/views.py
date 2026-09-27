from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai.api.serializers import AISummarySerializer
from momcare_platform.core.ai.models import AISummary
from momcare_platform.core.common.permissions import IsHospitalStaff
from momcare_platform.core.common.scoping import LocationScopedQuerysetMixin
from momcare_platform.core.patients.models import Patient


class PatientAISummaryView(LocationScopedQuerysetMixin, APIView):
    """Read the cached AI Summary for one patient. Never generates one on
    the fly -- see the design doc's Triggers section for the only paths
    that ever call generate_patient_summary()."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "organization"
    location_lookup = "location"

    def get(self, request, patient_id):
        patients = self.scope_to_locations(Patient.objects.all())
        patient = get_object_or_404(patients, pk=patient_id)
        summary = AISummary.objects.filter(patient=patient).first()
        if summary is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(AISummarySerializer(summary).data)
