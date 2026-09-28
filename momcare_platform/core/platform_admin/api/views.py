"""Platform-admin API. AI config is the first real capability this app has
had -- previously a documented empty skeleton (see CLAUDE.md)."""

from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.api.serializers import AIInstructionPresetSerializer
from momcare_platform.core.ai.models import AIInstructionPreset
from momcare_platform.core.ai.services import (
    InstructionPresetStateError,
    activate_instruction_preset,
    deactivate_instruction_preset,
    get_ai_config,
)
from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsPlatformAdmin
from momcare_platform.core.platform_admin.api.serializers import AIProviderConfigSerializer


class AIProviderConfigView(APIView):
    """Read/edit the platform-wide model, word cap, and instructions.
    ROLE_PLATFORM_ADMIN only -- not visible to any hospital-side role."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        config = get_ai_config()
        return Response(AIProviderConfigSerializer(config).data)

    def patch(self, request):
        config = get_ai_config()
        serializer = AIProviderConfigSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AIProviderConfigSerializer(config).data)


class AIAvailableModelsView(APIView):
    """The live OpenRouter catalog, for the platform-admin picker UI."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        catalog = openrouter_client.list_available_models() or []
        return Response(catalog)


class AIInstructionPresetListCreateView(APIView):
    """Platform-tier instruction preset history. List/create, never
    edit/delete -- see AIInstructionPreset's own docstring.

    create()'s INSERT (organization=None) only satisfies the RLS policy's
    WITH CHECK because TenantAwareJWTAuthentication enters bypass_rls() for
    a token with no org_id claim (a platform admin's), and SET LOCAL
    app.rls_bypass survives RELEASE SAVEPOINT for the rest of the request --
    not because of any Postgres-level exemption for this role (there is
    only one connecting role, momcare_app, NOBYPASSRLS). See
    docs/design/2026-09-28-ai-instruction-presets-design.md's Tenancy
    section for the full account; tightening bypass_rls() to scope itself
    more precisely would silently break this create() with no other code
    change."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        presets = AIInstructionPreset.objects.filter(organization__isnull=True)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(presets, request, view=self)
        return paginator.get_paginated_response(AIInstructionPresetSerializer(page, many=True).data)

    def post(self, request):
        serializer = AIInstructionPresetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(organization=None, created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AIInstructionPresetActivateView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request, preset_id):
        preset = get_object_or_404(AIInstructionPreset, pk=preset_id, organization__isnull=True)
        try:
            activate_instruction_preset(preset)
        except InstructionPresetStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AIInstructionPresetSerializer(preset).data)


class AIInstructionPresetDeactivateView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request, preset_id):
        preset = get_object_or_404(AIInstructionPreset, pk=preset_id, organization__isnull=True)
        try:
            deactivate_instruction_preset(preset)
        except InstructionPresetStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AIInstructionPresetSerializer(preset).data)
