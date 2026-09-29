"""Platform-admin API. AI config is the first real capability this app has
had -- previously a documented empty skeleton (see CLAUDE.md)."""

from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.api.serializers import (
    AISummaryTemplateSerializer,
    SummaryTemplateProposalRequestSerializer,
)
from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import (
    ActivationStateError,
    activate_summary_template,
    deactivate_summary_template,
    get_ai_config,
    propose_and_preview_template,
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


class AISummaryTemplateListCreateView(APIView):
    """Platform-tier summary template history -- layout arrangement and
    optional extra wording, saved and activated together. List/create, never
    edit/delete -- see AISummaryTemplate's own docstring.

    create()'s INSERT (organization=None) only satisfies the RLS policy's
    WITH CHECK because TenantAwareJWTAuthentication enters bypass_rls() for
    a token with no org_id claim (a platform admin's), and SET LOCAL
    app.rls_bypass survives RELEASE SAVEPOINT for the rest of the request --
    not because of any Postgres-level exemption for this role (there is
    only one connecting role, momcare_app, NOBYPASSRLS). See
    docs/design/2026-09-28-ai-instruction-presets-design.md's Tenancy
    section for the full account of this bypass_rls() mechanism (written
    for the now-retired preset system, but the mechanism is identical here
    since both go through the same TenantAwareJWTAuthentication path);
    tightening bypass_rls() to scope itself more precisely would silently
    break this create() with no other code change."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def get(self, request):
        templates = AISummaryTemplate.objects.filter(organization__isnull=True)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(templates, request, view=self)
        return paginator.get_paginated_response(AISummaryTemplateSerializer(page, many=True).data)

    def post(self, request):
        serializer = AISummaryTemplateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(organization=None, created_by=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class AISummaryTemplateActivateView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request, template_id):
        template = get_object_or_404(AISummaryTemplate, pk=template_id, organization__isnull=True)
        try:
            activate_summary_template(template)
        except ActivationStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AISummaryTemplateSerializer(template).data)


class AISummaryTemplateDeactivateView(APIView):
    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request, template_id):
        template = get_object_or_404(AISummaryTemplate, pk=template_id, organization__isnull=True)
        try:
            deactivate_summary_template(template)
        except ActivationStateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(AISummaryTemplateSerializer(template).data)


class AISummaryTemplateProposalView(APIView):
    """AI-assisted template authoring: describe a layout/wording preference
    in plain English, get back a candidate {sections, extra_instructions,
    preview_text}. Stateless -- nothing is saved here, calling it again
    ("regenerate") is just calling it again. The preview always uses fixed
    sample data, never a real patient -- identical at both tiers, since the
    platform tier has no single hospital's patient to reach for anyway."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request):
        serializer = SummaryTemplateProposalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = propose_and_preview_template(serializer.validated_data["description"])
        if result is None:
            return Response(
                {"detail": "Could not generate a template proposal right now. Try again."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(result)
