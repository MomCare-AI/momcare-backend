from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.permissions import IsHospitalAdmin
from momcare_platform.core.organization.api.serializers import (
    OrganizationPhotoUpdateSerializer,
    OrganizationSummarySerializer,
)


class MyOrganizationView(APIView):
    """The signed-in user's own hospital.

    Scoped by ``request.user.organization`` rather than by a URL id, so there is
    no tenant identifier a caller could tamper with to read another hospital.
    Platform admins have no organization and get 404 here — they work through
    the admin, not a hospital dashboard.

    Write access is deliberately narrow: only ``building_photo``, and only
    hospital_admin. Every other field here is either the evidence the
    hospital's approval rested on, or drives which population's clinical
    risk thresholds apply — see ``OrganizationPhotoUpdateSerializer``.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(
                {"detail": "This account is not attached to a hospital."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(OrganizationSummarySerializer(org, context={"request": request}).data)

    def patch(self, request):
        org = request.user.organization
        if org is None:
            return Response(
                {"detail": "This account is not attached to a hospital."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not IsHospitalAdmin().has_permission(request, self):
            return Response(
                {"detail": "Only a hospital administrator can update this."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = OrganizationPhotoUpdateSerializer(org, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(OrganizationSummarySerializer(org, context={"request": request}).data)
