from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.organization.api.serializers import OrganizationSummarySerializer


class MyOrganizationView(APIView):
    """The signed-in user's own hospital.

    Scoped by ``request.user.organization`` rather than by a URL id, so there is
    no tenant identifier a caller could tamper with to read another hospital.
    Platform admins have no organization and get 404 here — they work through
    the admin, not a hospital dashboard.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        org = request.user.organization
        if org is None:
            return Response(
                {"detail": "This account is not attached to a hospital."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(OrganizationSummarySerializer(org).data)
