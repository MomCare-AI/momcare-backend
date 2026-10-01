"""Platform-admin API. AI config is the first real capability this app has
had -- previously a documented empty skeleton (see CLAUDE.md)."""

from django.db import transaction
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.ai import openrouter_client
from momcare_platform.core.ai.api.serializers import (
    AISummaryTemplateSerializer,
    SummaryTemplateReviewRequestSerializer,
)
from momcare_platform.core.ai.models import AISummaryTemplate
from momcare_platform.core.ai.services import (
    TEMPLATE_FIELD_LABELS,
    ActivationStateError,
    activate_summary_template,
    check_template_coverage,
    deactivate_summary_template,
    get_ai_config,
    missing_fields_message,
    review_summary_template,
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
    """Summary template history -- plain text the platform admin writes. List/create, never edit/delete.
    Creating a template activates it immediately and deactivates the previous one -- see AISummaryTemplate's own docstring.

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
        missing = check_template_coverage(serializer.validated_data["content"])
        if missing is None:
            return Response(
                {"detail": "The service is not working right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if missing:
            return Response(
                {
                    "content": [missing_fields_message(missing)],
                    "missing_fields": missing,
                    "missing_labels": [TEMPLATE_FIELD_LABELS[f] for f in missing],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            template = serializer.save(organization=None, created_by=request.user)
            # Saving a new template makes it the one in use: it becomes the
            # active template and the previous one is switched off, together.
            activate_summary_template(template)
        return Response(AISummaryTemplateSerializer(template).data, status=status.HTTP_201_CREATED)


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


class AISummaryTemplateEnhanceView(APIView):
    """Review step for a draft template. The admin sends the plain-language
    ``content`` they wrote. The AI decides which of the 17 fields it covers: if
    any are missing the response is ``complete: false`` with
    ``missing_labels`` and a message to show as an alert. Once everything is
    covered, the AI polishes grammar and wording, and the response carries
    ``enhanced_content`` plus ``preview_text`` -- the entire summary as a
    patient would see it, rendered from fixed sample data (never a real
    patient). Stateless -- nothing is saved here."""

    permission_classes = [IsAuthenticated, IsPlatformAdmin]

    def post(self, request):
        serializer = SummaryTemplateReviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        content = serializer.validated_data["content"]
        result = review_summary_template(content)
        if result is None:
            return Response(
                {"detail": "The service is not working right now. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"content": content, **result})
