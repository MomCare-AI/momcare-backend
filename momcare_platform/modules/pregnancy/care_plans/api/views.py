"""Care plan endpoints -- see docs/design/2026-10-05-care-plan-design.md.

Reads are one aggregate response (``plan_payload``); writes are per section,
because permissions and validation differ (medication is provider-only, and
nutrition/exercise edits also feed the corrections log).

Every queryset is scoped through the requesting user's hospital *before* any
lookup, so another hospital's plan resolves to 404 -- never 403.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import (
    IsHospitalAdmin,
    IsHospitalStaff,
    IsPatient,
    IsProvider,
    user_role_code,
)
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
from momcare_platform.core.patients.models import Pregnancy

from .. import services
from ..models import (
    CarePlan,
    CarePlanAdjustment,
    CarePlanMedication,
    CarePlanNote,
    CareWeek,
    HospitalPreference,
)
from .serializers import (
    AdjustmentCreateSerializer,
    AdjustmentUpdateSerializer,
    AllergiesConditionsSerializer,
    GuidanceSerializer,
    OptionalGuidanceSerializer,
    ReasonSerializer,
    TextSerializer,
    items_section_payload,
    plan_payload,
    preference_payload,
    section_payload,
)

NOT_FOUND = {"detail": "Not found."}
_LOOKUP_ERRORS = (DjangoValidationError, ValueError)


def _conflict(exc) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)


def _bad(exc) -> Response:
    return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class CarePlanScopedView(OrganizationScopedQuerysetMixin, APIView):
    """Base for plan endpoints: hospital staff only, scoped through the
    pregnancy's patient's location to the hospital."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    organization_lookup = "pregnancy__patient__location__organization"

    def plans(self):
        return self.scope_to_organization(
            CarePlan.objects.select_related("pregnancy__patient__organization", "reviewed_by", "finalized_by")
        )

    def get_plan_or_404(self, plan_id):
        try:
            return self.plans().get(pk=plan_id), None
        except (CarePlan.DoesNotExist, *_LOOKUP_ERRORS):
            return None, Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)

    def get_child_or_404(self, model, plan, child_id):
        try:
            return model.objects.select_related("care_plan__pregnancy__patient").get(pk=child_id, care_plan=plan), None
        except (model.DoesNotExist, *_LOOKUP_ERRORS):
            return None, Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)


# -- reading -----------------------------------------------------------------


class CarePlanListView(CarePlanScopedView):
    def get(self, request):
        plans = self.plans().order_by("-period_start")
        pregnancy = request.query_params.get("pregnancy")
        if pregnancy:
            try:
                plans = plans.filter(pregnancy_id=pregnancy)
            except _LOOKUP_ERRORS:
                return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(plans, request, view=self)
        return paginator.get_paginated_response([plan_payload(p) for p in page])


class CarePlanDetailView(CarePlanScopedView):
    def get(self, request, plan_id):
        plan, missing = self.get_plan_or_404(plan_id)
        return missing or Response(plan_payload(plan))


class CurrentCarePlanView(OrganizationScopedQuerysetMixin, APIView):
    """The screen's entry point -- for staff on a patient's page and for the
    patient herself in the app. A patient only ever reaches her own pregnancy,
    and gets the same payload without staff bookkeeping."""

    permission_classes = [IsAuthenticated, IsHospitalStaff | IsPatient]
    organization_lookup = "patient__location__organization"

    # None: the whole plan; otherwise that section alone -- "nutrition"/"exercise" (generated)
    # or "medications"/"notes" (written by staff).
    section: str | None = None

    def get(self, request, pregnancy_id):
        is_patient = user_role_code(request.user) == settings.ROLE_PATIENT
        pregnancies = self.scope_to_organization(Pregnancy.objects.select_related("patient__organization"))
        if is_patient:
            pregnancies = pregnancies.filter(patient__user=request.user)
        try:
            pregnancy = pregnancies.get(pk=pregnancy_id)
        except (Pregnancy.DoesNotExist, *_LOOKUP_ERRORS):
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)

        # The plan holding the week she is in (a week belongs to the month containing its
        # first day), else the latest plan.
        today = services.local_today(pregnancy)
        plans = pregnancy.care_plans.select_related("reviewed_by", "finalized_by")
        week = (
            CareWeek.objects.filter(care_plan__pregnancy=pregnancy, week_start__lte=today, week_end__gte=today)
            .select_related("care_plan")
            .first()
        )
        plan = plans.filter(pk=week.care_plan_id).first() if week else plans.order_by("-period_start").first()
        # GET never creates anything: a plan appears when a reading generates it (in the
        # background, so a plan can be a few seconds behind the reading that asked for it).
        flags = services.plan_progress_flags(pregnancy, plan, week is not None)
        if plan is None:
            return Response({"care_plan": None, **flags})
        if self.section in ("medications", "notes"):
            return Response({"care_plan": items_section_payload(plan, self.section), **flags})
        if self.section:
            return Response({"care_plan": section_payload(plan, self.section, for_patient=is_patient), **flags})
        return Response({"care_plan": plan_payload(plan, for_patient=is_patient), **flags})


class CurrentNutritionView(CurrentCarePlanView):
    section = "nutrition"


class CurrentExerciseView(CurrentCarePlanView):
    section = "exercise"


class CurrentMedicationsView(CurrentCarePlanView):
    section = "medications"


class CurrentNotesView(CurrentCarePlanView):
    section = "notes"


class MyCarePlansMixin(OrganizationScopedQuerysetMixin):
    """A patient's own plans -- read-only, every month of her pregnancy.

    The staff routes (``/care-plans/``) stay staff-only; she gets her own
    ``/my-care-plans/`` so neither side's permissions are bent for the other. Scoped to her
    hospital first (her token carries it once she is onboarded) and then to herself, so
    another woman's plan -- even in the same hospital -- is "not found".
    """

    permission_classes = [IsAuthenticated, IsPatient]
    organization_lookup = "pregnancy__patient__location__organization"

    def my_plans(self):
        return self.scope_to_organization(
            CarePlan.objects.select_related("pregnancy__patient__organization", "reviewed_by", "finalized_by").filter(
                pregnancy__patient__user=self.request.user
            )
        )


class MyCarePlanListView(MyCarePlansMixin, APIView):
    def get(self, request):
        plans = self.my_plans().order_by("-period_start")
        pregnancy = request.query_params.get("pregnancy")
        if pregnancy:
            try:
                plans = plans.filter(pregnancy_id=pregnancy)
            except _LOOKUP_ERRORS:
                return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(plans, request, view=self)
        return paginator.get_paginated_response([plan_payload(p, for_patient=True) for p in page])


class MyCarePlanDetailView(MyCarePlansMixin, APIView):
    def get(self, request, plan_id):
        try:
            plan = self.my_plans().get(pk=plan_id)
        except (CarePlan.DoesNotExist, *_LOOKUP_ERRORS):
            return Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)
        return Response(plan_payload(plan, for_patient=True))


# -- staff edits on generated sections ---------------------------------------


class AdjustmentCreateView(CarePlanScopedView):
    def post(self, request, plan_id):
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return missing
        serializer = AdjustmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            services.create_adjustment(plan, request.user, **data)
        except services.PlanLocked as exc:
            return _conflict(exc)
        except services.InvalidEdit as exc:
            return _bad(exc)
        plan = self.plans().get(pk=plan.pk)
        return Response(plan_payload(plan), status=status.HTTP_201_CREATED)


class AdjustmentDetailView(CarePlanScopedView):
    def patch(self, request, plan_id, adjustment_id):
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return missing
        adjustment, gone = self.get_child_or_404(CarePlanAdjustment, plan, adjustment_id)
        if gone:
            return gone
        serializer = AdjustmentUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.update_adjustment(adjustment, request.user, serializer.validated_data["content"])
        except services.PlanLocked as exc:
            return _conflict(exc)
        except services.InvalidEdit as exc:
            return _bad(exc)
        return Response(plan_payload(self.plans().get(pk=plan.pk)))

    def delete(self, request, plan_id, adjustment_id):
        """Deactivates -- nothing is physically deleted. Deactivating a staff
        removal brings the generated item back."""
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return missing
        adjustment, gone = self.get_child_or_404(CarePlanAdjustment, plan, adjustment_id)
        if gone:
            return gone
        reason = ReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        try:
            services.deactivate(adjustment, request.user, reason.validated_data["reason"])
        except services.PlanLocked as exc:
            return _conflict(exc)
        return Response(plan_payload(self.plans().get(pk=plan.pk)))


# -- medications (provider only) and notes (all staff) -----------------------


class _TextRowCreateView(CarePlanScopedView):
    create: Any = None

    def post(self, request, plan_id):
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return missing
        serializer = TextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            type(self).create(plan, request.user, serializer.validated_data["text"])
        except services.PlanLocked as exc:
            return _conflict(exc)
        return Response(plan_payload(self.plans().get(pk=plan.pk)), status=status.HTTP_201_CREATED)


class _TextRowDetailView(CarePlanScopedView):
    model: Any = None
    id_kwarg = ""

    def _load(self, plan_id, **kwargs):
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return None, None, missing
        row, gone = self.get_child_or_404(self.model, plan, kwargs[self.id_kwarg])
        return plan, row, gone

    def patch(self, request, plan_id, **kwargs):
        plan, row, error = self._load(plan_id, **kwargs)
        if error:
            return error
        serializer = TextSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.save_text_row(row, serializer.validated_data["text"])
        except services.PlanLocked as exc:
            return _conflict(exc)
        return Response(plan_payload(self.plans().get(pk=plan.pk)))

    def delete(self, request, plan_id, **kwargs):
        plan, row, error = self._load(plan_id, **kwargs)
        if error:
            return error
        reason = ReasonSerializer(data=request.data)
        reason.is_valid(raise_exception=True)
        try:
            services.deactivate(row, request.user, reason.validated_data["reason"])
        except services.PlanLocked as exc:
            return _conflict(exc)
        return Response(plan_payload(self.plans().get(pk=plan.pk)))


class MedicationCreateView(_TextRowCreateView):
    permission_classes = [IsAuthenticated, IsProvider]
    create = staticmethod(services.add_medication)


class MedicationDetailView(_TextRowDetailView):
    permission_classes = [IsAuthenticated, IsProvider]
    model = CarePlanMedication
    id_kwarg = "medication_id"


class NoteCreateView(_TextRowCreateView):
    create = staticmethod(services.add_note)


class NoteDetailView(_TextRowDetailView):
    model = CarePlanNote
    id_kwarg = "note_id"


# -- allergies and conditions (write-through to the patient record) ----------


class AllergiesConditionsView(CarePlanScopedView):
    def patch(self, request, plan_id):
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return missing
        serializer = AllergiesConditionsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_allergies_conditions(plan, dict(serializer.validated_data))
        return Response(plan_payload(self.plans().get(pk=plan.pk)))


# -- status ------------------------------------------------------------------


class _TransitionView(CarePlanScopedView):
    action: Any = None

    def post(self, request, plan_id):
        plan, missing = self.get_plan_or_404(plan_id)
        if missing:
            return missing
        try:
            type(self).action(plan, request.user)
        except services.InvalidTransition as exc:
            return _conflict(exc)
        return Response(plan_payload(self.plans().get(pk=plan.pk)))


class ReviewView(_TransitionView):
    action = staticmethod(services.review)


class FinalizeView(_TransitionView):
    permission_classes = [IsAuthenticated, IsProvider | IsHospitalAdmin]
    action = staticmethod(services.finalize_plan)


class ReopenView(_TransitionView):
    permission_classes = [IsAuthenticated, IsProvider | IsHospitalAdmin]
    action = staticmethod(services.reopen)


# -- hospital preferences (hospital admin) -----------------------------------


class PreferenceScopedView(OrganizationScopedQuerysetMixin, APIView):
    permission_classes = [IsAuthenticated, IsHospitalAdmin]
    organization_lookup = "organization"

    def preferences(self):
        return self.scope_to_organization(HospitalPreference.objects.select_related("decided_by"))

    def get_or_404(self, preference_id):
        try:
            return self.preferences().get(pk=preference_id), None
        except (HospitalPreference.DoesNotExist, *_LOOKUP_ERRORS):
            return None, Response(NOT_FOUND, status=status.HTTP_404_NOT_FOUND)


class PreferenceListView(PreferenceScopedView):
    def get(self, request):
        prefs = self.preferences().order_by("-created_at")
        wanted = request.query_params.get("status")
        if wanted:
            prefs = prefs.filter(status=wanted)
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(prefs, request, view=self)
        return paginator.get_paginated_response([preference_payload(p) for p in page])


class PreferenceDetailView(PreferenceScopedView):
    def get(self, request, preference_id):
        pref, missing = self.get_or_404(preference_id)
        return missing or Response(preference_payload(pref))

    def patch(self, request, preference_id):
        """Reword a suggestion before approving it."""
        pref, missing = self.get_or_404(preference_id)
        if missing:
            return missing
        if pref.status != HospitalPreference.STATUS_SUGGESTED:
            return _conflict("Only a suggestion can be reworded.")
        serializer = GuidanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.set_guidance(pref, serializer.validated_data["guidance"])
        except services.InvalidEdit as exc:
            return _bad(exc)
        return Response(preference_payload(pref))


class _PreferenceDecisionView(PreferenceScopedView):
    new_status = ""

    def post(self, request, preference_id):
        pref, missing = self.get_or_404(preference_id)
        if missing:
            return missing
        serializer = OptionalGuidanceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.decide_preference(
                pref, request.user, status=self.new_status, guidance=serializer.validated_data.get("guidance")
            )
        except services.InvalidTransition as exc:
            return _conflict(exc)
        except services.InvalidEdit as exc:
            return _bad(exc)
        return Response(preference_payload(pref))


class PreferenceApproveView(_PreferenceDecisionView):
    new_status = HospitalPreference.STATUS_APPROVED


class PreferenceRejectView(_PreferenceDecisionView):
    new_status = HospitalPreference.STATUS_REJECTED


class PreferenceDeactivateView(_PreferenceDecisionView):
    new_status = HospitalPreference.STATUS_INACTIVE
