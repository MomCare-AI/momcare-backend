"""Patient and pregnancy endpoints.

Every queryset here is scoped through ``location__organization`` by the shared
tenancy mixin. Nothing accepts an organization or location from the client:
tenant membership is taken from the authenticated user, so there is no
identifier a caller could tamper with to reach another hospital's patients.
"""

import importlib
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Concat
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.analytics.services import (
    patients_needing_monitoring_follow_up,
    patients_needing_reading_reminder,
    patients_with_unseen_readings,
)
from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsCareManager, IsHospitalAdmin, IsHospitalStaff, IsPatient
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.common.scoping import (
    OrganizationScopedQuerysetMixin,
    scope_to_assigned_staff,
)
from momcare_platform.core.locations.models import Location
from momcare_platform.core.monitoring.models import PatientStatus
from momcare_platform.core.patients import profile as patient_profile
from momcare_platform.core.patients.api.serializers import (
    MyProfileSerializer,
    PatientCreateSerializer,
    PatientDetailSerializer,
    PatientJoinRequestSerializer,
    PatientListSerializer,
    PregnancySerializer,
    PregnancyWriteSerializer,
    WorklistPatientSerializer,
    attach_risk_this_month,
)
from momcare_platform.core.patients.models import Patient, PatientJoinRequest, Pregnancy
from momcare_platform.core.patients.services import (
    AlreadyOnboardedError,
    OnboardingError,
    create_pregnancy,
    deactivate_patient,
    onboard_from_join_request,
    onboard_patient,
    reactivate_patient,
)
from momcare_platform.core.staff.models import Staff

NO_HOSPITAL = {"detail": "This account is not attached to a hospital."}


class PatientScopedView(OrganizationScopedQuerysetMixin, APIView):
    """Base for patient endpoints — tenant-scoped, hospital staff only."""

    permission_classes = [IsAuthenticated, IsHospitalStaff]
    # Patient reaches its hospital through Location, so the scope walks that FK.
    organization_lookup = "location__organization"

    def hospital_or_error(self, request):
        org = request.user.organization
        if org is None:
            return None, Response(NO_HOSPITAL, status=status.HTTP_404_NOT_FOUND)
        return org, None

    def patients(self):
        return self.scope_to_organization(Patient.objects.all())

    def apply_roster_filters(self, queryset, request):
        """``?location=``/``?is_active=``/``?care_manager=``/``?provider=``/
        ``?nurse=`` -- shared by the Patient List and Dashboard KPIs so the
        two can never disagree about which patients are even in scope,
        matching Neuro_RPM's own ``_scoped_queryset()`` reuse between its
        Patient List and ``dashboard_kpis`` action for the identical reason.

        ``care_manager``/``provider``/``nurse`` reach through the active
        pregnancy (that's where the care team actually lives -- see
        CLAUDE.md's "why staff attaches to Pregnancy, not Patient" for why),
        the same ``pregnancies__`` join ``?assigned_to=me`` already uses.
        """
        params = request.query_params
        location = params.get("location")
        if location:
            queryset = queryset.filter(location_id=location)

        is_active = params.get("is_active")
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() in ("true", "1", "yes"))

        needs_distinct = False
        for role_field in ("care_manager", "provider", "nurse"):
            value = params.get(role_field)
            if value:
                queryset = queryset.filter(
                    pregnancies__status=Pregnancy.STATUS_ACTIVE,
                    **{f"pregnancies__{role_field}_id": value},
                )
                needs_distinct = True

        return queryset.distinct() if needs_distinct else queryset

    def get_patient_or_404(self, patient_id):
        """Scope first, then look up.

        A patient in another hospital resolves to nothing rather than being
        found and then refused — the 404 is identical either way, so the API
        never reveals that a patient exists elsewhere.
        """
        # A malformed UUID raises ValidationError; that is a bad identifier, not
        # a server fault, so it reads as "not found" like any other miss.
        try:
            return (
                self.patients().select_related("location", "user").get(pk=patient_id),
                None,
            )
        except Patient.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)


def _active_pregnancy_prefetch() -> Prefetch:
    """The active pregnancy for each listed patient, carrying its latest risk level.

    Two things are being avoided here. ``current_pregnancy`` queries once per
    patient, so a page of twenty costs twenty round trips; and reading the
    latest assessment through the related manager would cost twenty more. Both
    collapse into one prefetch with a correlated subquery.

    Resolved via the app registry, not a static import: ``RiskAssessment``
    lives in ``modules.pregnancy.vitals``, a module `core` must never import
    (the `core must not import modules` import-linter contract) — the same
    pattern Neuro_RPM uses for its own core-to-module model lookups (e.g.
    ``CarePlan``, resolved via ``apps.get_model`` for the identical reason).
    A runtime lookup isn't a Python import statement, so the contract's
    static analysis never sees it.
    """
    from django.apps import apps as django_apps  # noqa: PLC0415

    RiskAssessment = django_apps.get_model("monitoring", "RiskAssessment")

    latest = RiskAssessment.objects.filter(pregnancy=OuterRef("pk")).order_by("-assessed_at")
    # Same "needs attention" condition the old RiskReviewQueueView/
    # LowConfidenceQueueView used before they were consolidated into
    # ?workflow= query params -- see PatientListSerializer's own
    # pending_risk_count/needs_risk_review/needs_low_confidence_review for
    # why these are annotated unconditionally on every row, matching
    # Neuro_RPM's own "merge everything onto every row" convention.
    pending_actionable = RiskAssessment.objects.filter(
        pregnancy=OuterRef("pk"),
        review_status=RiskAssessment.REVIEW_PENDING,
        final_risk_level__in=[RiskAssessment.LEVEL_MEDIUM, RiskAssessment.LEVEL_HIGH],
    )
    pending_flagged = RiskAssessment.objects.filter(
        pregnancy=OuterRef("pk"),
        review_status=RiskAssessment.REVIEW_PENDING,
        flagged_for_review=True,
    )
    pending_either = RiskAssessment.objects.filter(
        Q(final_risk_level__in=[RiskAssessment.LEVEL_MEDIUM, RiskAssessment.LEVEL_HIGH]) | Q(flagged_for_review=True),
        pregnancy=OuterRef("pk"),
        review_status=RiskAssessment.REVIEW_PENDING,
    )

    def _count_subquery(qs):
        return Subquery(
            qs.order_by().values("pregnancy").annotate(_count=Count("id")).values("_count"),
            output_field=IntegerField(),
        )

    active = (
        Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE)
        .select_related("provider__user", "nurse__user", "care_manager__user")
        .annotate(
            # final_risk_level: the level actually acted on, not the model's
            # pre-escalation answer.
            latest_risk_level=Subquery(latest.values("final_risk_level")[:1]),
            latest_risk_at=Subquery(latest.values("assessed_at")[:1]),
            pending_actionable_count=_count_subquery(pending_actionable),
            pending_flagged_count=_count_subquery(pending_flagged),
            pending_risk_count=_count_subquery(pending_either),
        )
        .order_by("-created_at")
    )
    return Prefetch("pregnancies", queryset=active, to_attr="active_pregnancies")


def _monitoring_analytics_prefetch() -> Prefetch:
    """This calendar month's ``PatientAnalytics`` row for each listed
    patient, so ``PatientListSerializer`` can show
    ``monitoring_seconds_this_month`` without a query per row -- same
    reasoning as ``_active_pregnancy_prefetch`` above. A plain import, not
    the app-registry lookup that model needs: ``core.analytics`` is a core
    app, so ``core.patients`` importing it directly doesn't touch the
    `core must not import modules` contract at all.
    """
    from momcare_platform.core.analytics.models import PatientAnalytics  # noqa: PLC0415

    current_period = timezone.now().date().replace(day=1)
    return Prefetch(
        "analytics_periods",
        queryset=PatientAnalytics.objects.filter(period_month=current_period),
        to_attr="current_period_analytics",
    )


def _statuses_prefetch():
    """Every status entry, newest first -- ``PatientListSerializer`` embeds
    the whole history per row (see the patient-statuses design doc), so this
    avoids one query per patient on a paginated list."""
    return Prefetch(
        "statuses",
        queryset=PatientStatus.objects.order_by("-created_at"),
        to_attr="prefetched_statuses",
    )


class PatientListCreateView(PatientScopedView):
    """List and search this hospital's patients, or enrol a new one."""

    def _scope_to_assigned(self, queryset, request):
        """``?assigned_to=me`` on a Patient queryset — see
        ``scope_to_assigned_staff``'s own docstring (core/common/scoping.py)
        for the shared semantics this delegates to.
        """
        return scope_to_assigned_staff(queryset, request, path_prefix="pregnancies__")

    def _apply_workflow_and_care_activity(self, queryset, request):
        """``?workflow=risk_review``/``?workflow=low_confidence`` and
        ``?care_activity=monitoring_follow_up``/``unseen_readings``/
        ``reading_reminder`` -- one endpoint, five filters, matching
        Neuro_RPM's own query-param-on-one-endpoint convention (the five
        standing queue endpoints this replaced are gone -- see CLAUDE.md's
        "Care Activities" section for the history). The two are
        independent, parallel filters, like Neuro_RPM's own -- never
        combined into one condition, and only one of each may be passed.

        ``risk_review``/``low_confidence`` are resolved via
        ``importlib``, not a static import: their condition lives in
        ``modules.pregnancy.vitals``, which `core` must never import.
        """
        workflow = request.query_params.get("workflow")
        if workflow:
            vitals_services = importlib.import_module("momcare_platform.modules.pregnancy.vitals.services")
            if workflow == "risk_review":
                queryset = vitals_services.patients_needing_risk_review(queryset)
            elif workflow == "low_confidence":
                queryset = vitals_services.patients_needing_low_confidence_review(queryset)
            elif workflow in ("care_plan_review", "care_plan_missing"):
                care_plan_services = importlib.import_module("momcare_platform.modules.pregnancy.care_plans.services")
                queryset = (
                    care_plan_services.patients_needing_care_plan_review(queryset)
                    if workflow == "care_plan_review"
                    else care_plan_services.patients_missing_care_plan(queryset)
                )

        care_activity = request.query_params.get("care_activity")
        if care_activity == "monitoring_follow_up":
            queryset = patients_needing_monitoring_follow_up(queryset)
        elif care_activity in ("unseen_readings", "reading_reminder"):
            # Both require an active pregnancy -- a VitalReading always has
            # one, unlike MonitoringSession/MonitoringNote above.
            queryset = queryset.filter(pregnancies__status=Pregnancy.STATUS_ACTIVE).distinct()
            queryset = (
                patients_with_unseen_readings(queryset)
                if care_activity == "unseen_readings"
                else patients_needing_reading_reminder(queryset)
            )

        return queryset

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        queryset = (
            self.patients()
            .select_related("location")
            .prefetch_related(
                _active_pregnancy_prefetch(),
                _statuses_prefetch(),
                _monitoring_analytics_prefetch(),
            )
        )
        queryset = self._scope_to_assigned(queryset, request)
        queryset = self.apply_roster_filters(queryset, request)
        queryset = self._apply_workflow_and_care_activity(queryset, request)

        search = request.query_params.get("search", "").strip()
        if search:
            # Server-side: the client never receives rows it then filters away,
            # which would mean shipping the whole patient list to the browser.
            queryset = queryset.annotate(
                _full_name=Concat("first_name", Value(" "), "last_name"),
            ).filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(_full_name__icontains=search)
                | Q(phone__icontains=search)
                | Q(national_id__icontains=search)
                | Q(mrn__icontains=search),
            )

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset.order_by("-created_at", "id"), request, view=self)
        attach_risk_this_month(page)
        return paginator.get_paginated_response(PatientListSerializer(page, many=True).data)

    @staticmethod
    def _join_request_or_refusal(org, raw_id):
        """The pending join request this onboarding form answers, or the refusal to send.

        ``(None, None)`` when the form is not about a join request at all (a walk-in).
        An id that is not a UUID is left for the serializer to report as a 400.
        Looked up inside this hospital only, so a request addressed to another
        hospital is "not found", never "found and refused".
        """
        if not raw_id:
            return None, None
        try:
            request_uuid = uuid.UUID(str(raw_id))
        except ValueError:
            return None, None
        join_request = (
            PatientJoinRequest.objects.select_related("user", "location")
            .filter(pk=request_uuid, organization=org)
            .first()
        )
        if join_request is None:
            return None, Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        if not join_request.is_pending:
            return None, Response(
                {"detail": f"This request was already {join_request.status}."},
                status=status.HTTP_409_CONFLICT,
            )
        return join_request, None

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        # The request is resolved BEFORE the form is validated. A second tap on Save
        # sends the very same form, and validating it first would answer "national ID
        # already registered" -- true only because the first save worked -- instead of
        # saying the request is already decided.
        join_request, refusal = self._join_request_or_refusal(org, request.data.get("join_request"))
        if refusal:
            return refusal

        # Context carries the request so the nested care-team fields can
        # narrow its queryset to this hospital's own clinicians.
        serializer = PatientCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        patient_data, pregnancy_data = serializer.split()

        try:
            if join_request is not None:
                patient = onboard_from_join_request(
                    join_request=join_request,
                    organization=org,
                    patient_data=patient_data,
                    pregnancy_data=pregnancy_data,
                    decided_by=request.user,
                )
            else:
                patient = onboard_patient(
                    organization=org,
                    patient_data=patient_data,
                    pregnancy_data=pregnancy_data,
                )
        except AlreadyOnboardedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except OnboardingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PatientDetailSerializer(patient).data, status=status.HTTP_201_CREATED)


class PatientDashboardKpisView(PatientScopedView):
    """The single Dashboard KPIs endpoint -- MomCare's own three
    workflows/care-activities plus the roster's total/active/inactive
    split, matching Neuro_RPM's own single ``dashboard-kpis`` surface. No
    Priority Patients (their billing-cycle Priority Score has no MomCare
    equivalent and was explicitly declined) and no Manage Care Plans
    (MomCare has no CCM care-plan concept).

    ``total_patients``/``active_patients``/``inactive_patients`` and all
    five counts below share the identical scoping the Patient List uses --
    ``apply_roster_filters`` (``?location=``/``?is_active=``/
    ``?care_manager=``/``?provider=``/``?nurse=``) plus ``?assigned_to=me``
    -- via the same underlying condition functions
    ``?workflow=``/``?care_activity=`` filter the list by, so a number here
    can never disagree with what actually shows up if you applied that same
    filter on the list itself.

    ``risk_review``/``low_confidence`` are resolved via ``importlib``, not
    a static import: their condition lives in ``modules.pregnancy.vitals``,
    which `core` must never import (the `core must not import modules`
    contract); the three Care Activity conditions are plain static imports
    since ``core.analytics`` is itself a core app.

    ``pending_join_requests`` is a top-level sibling, not nested in
    ``workflow``/``care_activities`` -- a ``PatientJoinRequest`` isn't a
    ``Patient`` at all yet (no row exists until a hospital approves it), so
    it doesn't belong to either the workflow (no ``RiskAssessment``) or
    Care Activity (no `Patient` to filter) shape. Deliberately NOT run
    through ``apply_roster_filters``/``?assigned_to=me`` either -- those
    filters narrow the existing patient roster, and a join request has no
    location/care-team assignment yet for them to apply to. Scoped only by
    organization, same as ``JoinRequestReviewView``'s own queue.
    """

    def get(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        roster = self.apply_roster_filters(self.patients(), request)
        roster = scope_to_assigned_staff(roster, request, path_prefix="pregnancies__")
        counts = roster.aggregate(
            active_patients=Count("pk", filter=Q(is_active=True)),
            inactive_patients=Count("pk", filter=Q(is_active=False)),
        )
        active_patients = counts["active_patients"]
        inactive_patients = counts["inactive_patients"]
        pending_join_requests = PatientJoinRequest.objects.filter(
            organization=org,
            status=PatientJoinRequest.STATUS_PENDING,
        ).count()

        vitals_services = importlib.import_module("momcare_platform.modules.pregnancy.vitals.services")
        care_plan_services = importlib.import_module("momcare_platform.modules.pregnancy.care_plans.services")

        return Response(
            {
                "total_patients": active_patients + inactive_patients,
                "active_patients": active_patients,
                "inactive_patients": inactive_patients,
                "pending_join_requests": pending_join_requests,
                "risk": vitals_services.risk_level_breakdown(roster),
                "workflow": {
                    "risk_review": vitals_services.patients_needing_risk_review(roster).count(),
                    "low_confidence": vitals_services.patients_needing_low_confidence_review(roster).count(),
                    "care_plan_review": care_plan_services.patients_needing_care_plan_review(roster).count(),
                    "care_plan_missing": care_plan_services.patients_missing_care_plan(roster).count(),
                },
                "care_activities": {
                    "monitoring_follow_up": patients_needing_monitoring_follow_up(roster).count(),
                    "unseen_readings": patients_with_unseen_readings(
                        roster.filter(pregnancies__status=Pregnancy.STATUS_ACTIVE).distinct(),
                    ).count(),
                    "reading_reminder": patients_needing_reading_reminder(
                        roster.filter(pregnancies__status=Pregnancy.STATUS_ACTIVE).distinct(),
                    ).count(),
                },
            },
        )


class PatientQuickLookupKpisView(PatientScopedView):
    """``{"staff": {"total","active","inactive"}, "patients":
    {"total","active","inactive"}}`` -- organization-wide counts across
    every location, matching Neuro_RPM's own ``quick-lookup-kpis``.

    Deliberately separate from ``dashboard-kpis`` above, which is always
    scoped to the caller's own ``?location=``/``?assigned_to=me`` selection
    -- this one never is, for any caller, regardless of role or location
    assignment. **Adapted for multi-tenancy, not ported literally**:
    Neuro_RPM's own version applies *zero* scoping at all
    (``Patient.objects.aggregate(...)``, no organization filter), because
    that codebase is single-tenant -- one hospital per deployment, no
    `Organization` model to filter by. MomCare is shared-schema
    multi-tenant, so "no location/role scoping" here means skip
    ``apply_roster_filters``/``?assigned_to=me`` only -- the organization
    filter itself is never optional, dropping it would be a cross-tenant
    PHI leak, the one thing this project treats as non-negotiable
    regardless of what a reference platform's own single-tenant version
    does.
    """

    def get(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        patient_counts = Patient.objects.filter(organization=org).aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
            inactive=Count("pk", filter=Q(is_active=False)),
        )
        staff_counts = Staff.objects.filter(user__organization=org).aggregate(
            total=Count("pk"),
            active=Count("pk", filter=Q(is_active=True)),
            inactive=Count("pk", filter=Q(is_active=False)),
        )
        return Response({"staff": staff_counts, "patients": patient_counts})


class PatientWorklistView(PatientScopedView):
    """Administrative and care-continuity gaps — a different question from
    the Risk Review Queue's clinical severity.

    The Risk Review Queue (modules/pregnancy/vitals/api/views.py,
    RiskReviewQueueView) answers "whose vitals just crossed a threshold."
    This answers "does this case have a gap that has nothing to do with
    today's vitals being bad" - no reading in a while, no note logged, no
    risk history ever answered, nobody accountable. Deliberately a separate
    endpoint and never merged with the Risk Review Queue, the same way "not
    assessed" stays visually distinct from "low risk" everywhere else in
    this portal - see docs/worklist-feature-scope.md for the full reasoning.

    The two day thresholds below are administrative defaults, not clinically
    validated - the same honesty momcare_model applies to its own risk
    thresholds. Worth revisiting alongside the obstetrician review
    (PLAN.md §3 item 3), not asserted as correct here.
    """

    organization_lookup = "patient__location__organization"

    NO_READING_AFTER = timedelta(days=7)

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        # Resolved via the app registry, not a static import — same reason
        # as _active_pregnancy_prefetch above: VitalReading lives in
        # modules.pregnancy.vitals, which core must never import statically.
        from django.apps import apps as django_apps  # noqa: PLC0415

        VitalReading = django_apps.get_model("monitoring", "VitalReading")

        pregnancies = self.scope_to_organization(
            Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE),
        ).select_related("patient", "provider__user")
        pregnancies = scope_to_assigned_staff(pregnancies, request, path_prefix="")

        latest_reading = VitalReading.objects.filter(pregnancy=OuterRef("pk")).order_by("-recorded_at")
        pregnancies = pregnancies.annotate(
            latest_reading_at=Subquery(latest_reading.values("recorded_at")[:1]),
        )

        now = timezone.now()
        rows = []
        for pregnancy in pregnancies:
            reasons = self._reasons_for(pregnancy, now)
            if not reasons:
                continue
            rows.append(
                {
                    "patient_id": pregnancy.patient_id,
                    "pregnancy_id": pregnancy.id,
                    "full_name": pregnancy.patient.full_name,
                    "gestational_age": pregnancy.gestational_age_display,
                    "reasons": reasons,
                },
            )

        # Most gaps first - a case missing three things is more worth
        # opening than one missing a single, possibly-explainable thing.
        rows.sort(key=lambda r: -len(r["reasons"]))

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        return paginator.get_paginated_response(WorklistPatientSerializer(page, many=True).data)

    def _reasons_for(self, pregnancy, now) -> list[dict]:
        reasons = []

        reading_gap = self._days_since(pregnancy.latest_reading_at, now)
        if reading_gap is None or reading_gap >= self.NO_READING_AFTER.days:
            reasons.append(
                {
                    "code": "no_recent_reading",
                    "detail": (
                        f"No reading in {reading_gap} days."
                        if reading_gap is not None
                        else "No reading has ever been recorded."
                    ),
                    "days": reading_gap,
                },
            )

        if len(pregnancy.unanswered_factors) == len(Pregnancy.FACTOR_FIELDS):
            reasons.append(
                {
                    "code": "no_risk_history",
                    "detail": "No obstetric risk history has ever been answered.",
                    "days": None,
                },
            )

        if not pregnancy.has_responsible_clinician:
            reasons.append(
                {
                    "code": "no_lead_clinician",
                    "detail": "No lead clinician assigned.",
                    "days": None,
                },
            )

        return reasons

    @staticmethod
    def _days_since(at, now) -> int | None:
        return (now - at).days if at is not None else None


class PatientDetailView(PatientScopedView):
    def get(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        return missing or Response(PatientDetailSerializer(patient).data)

    def patch(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        serializer = PatientDetailSerializer(patient, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class PregnancyListCreateView(PatientScopedView):
    """A patient's pregnancy history, and opening a new episode."""

    def get(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        pregnancies = patient.pregnancies.select_related(
            "provider__user",
            "nurse__user",
            "care_manager__user",
        )
        paginator = DefaultPagination()
        page = paginator.paginate_queryset(pregnancies, request, view=self)
        return paginator.get_paginated_response(PregnancySerializer(page, many=True).data)

    def post(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        serializer = PregnancyWriteSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)

        try:
            pregnancy = create_pregnancy(patient=patient, data=data)
        except OnboardingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PregnancySerializer(pregnancy).data, status=status.HTTP_201_CREATED)


class PregnancyDetailView(PatientScopedView):
    """Read or correct one pregnancy. Deliberately no DELETE — a pregnancy is
    historical clinical fact, corrected rather than removed."""

    def _get(self, patient, pregnancy_id):
        try:
            return patient.pregnancies.get(pk=pregnancy_id), None
        except Pregnancy.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Pregnancy not found."}, status=status.HTTP_404_NOT_FOUND)

    def get(self, request, patient_id, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing
        pregnancy, gone = self._get(patient, pregnancy_id)
        return gone or Response(PregnancySerializer(pregnancy).data)

    def patch(self, request, patient_id, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing
        pregnancy, gone = self._get(patient, pregnancy_id)
        if gone:
            return gone

        serializer = PregnancyWriteSerializer(
            pregnancy,
            data=request.data,
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(PregnancySerializer(serializer.instance).data)


class PatientDeactivateView(PatientScopedView):
    """Deactivate a patient — never delete. Clinical records survive.

    Hospital admin or care manager only — matches Neuro_RPM's own permission
    for the identical action exactly (``MANAGE = IsAdmin | IsCareManager`` on
    their ``deactivate``/``reactivate``/``bulk_reassign``). Found via
    permission audit to be a real gap: this view had inherited the base
    ``PatientScopedView``'s plain ``IsHospitalStaff`` (any staff), which is
    correct for read/create/update but too broad for deactivation — a
    Provider or Nurse could otherwise take a patient off an active roster
    unilaterally.
    """

    permission_classes = [IsAuthenticated, (IsHospitalAdmin | IsCareManager)]

    def post(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        deactivate_patient(patient, by=request.user, reason=request.data.get("reason", ""))
        return Response(PatientDetailSerializer(patient).data)


class PatientReactivateView(PatientScopedView):
    """Undo a deactivation. Same permission as deactivate — see its own
    docstring."""

    permission_classes = [IsAuthenticated, (IsHospitalAdmin | IsCareManager)]

    def post(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        reactivate_patient(patient)
        return Response(PatientDetailSerializer(patient).data)


class PatientSelfView(APIView):
    """Base for the endpoints a self-registered woman calls herself.

    She has no organization, so her token carries no ``org_id`` claim and
    Postgres RLS — correctly fail-closed — would show her nothing at all.
    These views therefore run inside ``bypass_rls()`` and filter on
    ``user=request.user`` instead. The filter is doing the security work
    here, not the policy, which is why every queryset below is written
    against her own rows explicitly.
    """

    permission_classes = [IsAuthenticated, IsPatient]

    def my_requests(self, request):
        return PatientJoinRequest.objects.filter(user=request.user).select_related("organization", "patient")


class MyProfileView(PatientSelfView):
    """Her own details, filled in once and reused by every join request.

    Reads and writes only request.user's rows, so there is no identifier in the
    URL or body that could point at somebody else's profile.
    """

    def get(self, request):
        return Response(patient_profile.payload(request.user))

    def patch(self, request):
        serializer = MyProfileSerializer(data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        patient_profile.save_profile(request.user, serializer.validated_data)
        return Response(patient_profile.payload(request.user))


class HospitalDirectoryView(APIView):
    """The hospitals a woman can ask to join.

    Only approved, active ones: an application still under review is not
    something she should be able to send herself to.

    Deliberately a searchable list rather than distance-sorted. Real
    "hospitals near me" needs coordinates the Location model does not carry,
    and inventing them would be worse than a city filter that is honest
    about what it is.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from momcare_platform.core.organization.models import Organization

        # Cross-tenant by design and by necessity: she belongs to no hospital,
        # and the whole point is to show her the ones she could join. Only
        # public-facing fields are returned — never counts, licences or staff.
        paginator = DefaultPagination()
        with bypass_rls():
            hospitals = Organization.objects.filter(
                status=Organization.STATUS_APPROVED,
                is_active=True,
            )
            search = request.query_params.get("search", "").strip()
            if search:
                hospitals = hospitals.filter(Q(name__icontains=search) | Q(city__icontains=search))
            city = request.query_params.get("city", "").strip()
            if city:
                hospitals = hospitals.filter(city__iexact=city)

            hospitals = hospitals.prefetch_related(
                Prefetch("locations", queryset=Location.objects.filter(is_active=True).order_by("name", "id")),
            )
            page = paginator.paginate_queryset(hospitals.order_by("name", "id"), request, view=self)
            rows = [
                {
                    "id": str(h.id),
                    "name": h.name,
                    "city": h.city,
                    "country": h.country,
                    "phone": h.phone,
                    # Her choice of branch. Only what she needs to pick one: no
                    # manager, no counts. Empty for a hospital that has not set
                    # one up yet -- it then admits her to its default location.
                    "locations": [
                        {"id": str(loc.id), "name": loc.name, "city": loc.city, "state": loc.state}
                        for loc in h.locations.all()
                    ],
                }
                for h in page
            ]
        return paginator.get_paginated_response(rows)


class PatientJoinRequestView(PatientSelfView):
    """Ask a hospital to take her on, and see what she has already asked."""

    def get(self, request):
        paginator = DefaultPagination()
        with bypass_rls():
            page = paginator.paginate_queryset(self.my_requests(request), request, view=self)
            data = PatientJoinRequestSerializer(page, many=True).data
        return paginator.get_paginated_response(data)

    def post(self, request):
        from momcare_platform.core.organization.models import Organization

        # Her profile is what travels, so it has to be whole before anything is
        # sent: a hospital should never be handed a request it cannot act on.
        missing = patient_profile.missing_fields(request.user)
        if missing:
            return Response(
                {"detail": "Complete your profile before sending a request.", "missing_fields": missing},
                status=status.HTTP_400_BAD_REQUEST,
            )

        organization_id = request.data.get("organization")
        if not organization_id:
            return Response(
                {"organization": ["Choose a hospital to send this to."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with bypass_rls():
            try:
                hospital = Organization.objects.filter(
                    pk=organization_id,
                    status=Organization.STATUS_APPROVED,
                    is_active=True,
                ).first()
            except DjangoValidationError, ValueError:
                hospital = None
            if hospital is None:
                # The same 404 whether the id is wrong or the hospital is not
                # approved — a rejected application is not hers to discover.
                return Response({"detail": "Hospital not found."}, status=status.HTTP_404_NOT_FOUND)

            location = None
            location_id = request.data.get("location")
            if location_id:
                # Only an active branch of THIS hospital: a branch id from anywhere
                # else would otherwise put her on another tenant's patient list.
                try:
                    location = Location.objects.filter(pk=location_id, organization=hospital, is_active=True).first()
                except DjangoValidationError, ValueError:
                    location = None
                if location is None:
                    return Response(
                        {"location": ["Choose one of this hospital's branches."]},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            already_waiting = (
                self.my_requests(request)
                .filter(organization=hospital, status=PatientJoinRequest.STATUS_PENDING)
                .exists()
            )
            if already_waiting:
                return Response(
                    {"detail": "You already have a request waiting with this hospital."},
                    status=status.HTTP_409_CONFLICT,
                )

            join_request = PatientJoinRequest.objects.create(
                user=request.user,
                organization=hospital,
                location=location,
                # Built here from her own profile, never from the request body:
                # the hospital reads this to decide, so she cannot be allowed to
                # hand-write it.
                draft=patient_profile.snapshot(request.user),
            )
            body = PatientJoinRequestSerializer(join_request).data
        return Response(body, status=status.HTTP_201_CREATED)


class JoinRequestWithdrawView(PatientSelfView):
    """She takes back a request she has not had answered yet.

    Only her own, and only while still pending: once a hospital has approved
    it there is a real ``Patient`` row and a care relationship, which is not
    something a POST from the phone should unpick — she would have to ask the
    hospital to discharge her. Withdrawing a rejected request would also
    quietly rewrite the hospital's record of a decision it made.

    Deliberately no edit endpoint alongside this. The ``draft`` is the thing
    the hospital reads when deciding; letting her rewrite it after submitting
    means staff could approve details they never saw. Withdraw and send a new
    one — same outcome, and the hospital always acts on what it was shown.
    """

    def post(self, request, request_id):
        with bypass_rls():
            try:
                join_request = self.my_requests(request).filter(pk=request_id).first()
            except DjangoValidationError, ValueError:
                join_request = None

            # Scoped to her own rows before the lookup, so another woman's
            # request is "not found" rather than found and refused.
            if join_request is None:
                return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)

            if join_request.status != PatientJoinRequest.STATUS_PENDING:
                return Response(
                    {
                        "detail": (
                            f"This request was already {join_request.get_status_display().lower()} "
                            "and can no longer be withdrawn."
                        ),
                        "status": join_request.status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            join_request.status = PatientJoinRequest.STATUS_WITHDRAWN
            join_request.decided_at = timezone.now()
            join_request.decision_note = request.data.get("note", "")
            join_request.save(update_fields=["status", "decided_at", "decision_note", "updated_at"])

            return Response(PatientJoinRequestSerializer(join_request).data)


def _review_row(join_request) -> dict:
    return {
        **PatientJoinRequestSerializer(join_request).data,
        "applicant_email": join_request.user.email,
        "applicant_name": join_request.user.get_full_name(),
    }


class JoinRequestBaseView(PatientScopedView):
    """Scoping shared by the hospital's queue, one request, and its reject action.

    Deliberately a sibling base rather than the other views subclassing the
    queue view: their URLs pass different kwargs (``request_id`` vs none), so
    inheriting the queue's ``get`` made ``GET`` on a reject URL raise TypeError
    and return 500 instead of a clean 405.
    """

    organization_lookup = "organization"

    def requests(self):
        return self.scope_to_organization(PatientJoinRequest.objects.all()).select_related("user", "organization")


class JoinRequestReviewView(JoinRequestBaseView):
    """The hospital's side: who is asking to join.

    Scoped through the request's own ``organization`` column, so one hospital
    never sees another's queue.
    """

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        queryset = self.requests()
        state = request.query_params.get("status", "").strip()
        if state:
            queryset = queryset.filter(status=state)

        rows = [_review_row(r) for r in queryset.order_by("-created_at")]
        return Response({"count": len(rows), "results": rows})


class JoinRequestDetailView(JoinRequestBaseView):
    """One request, with her profile snapshot: what pre-fills the onboarding form."""

    def get(self, request, request_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        try:
            join_request = self.requests().get(pk=request_id)
        except PatientJoinRequest.DoesNotExist, DjangoValidationError, ValueError:
            return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_review_row(join_request))


class JoinRequestRejectView(JoinRequestBaseView):
    """Decline a request. Approving is not here: saving the onboarding form is the approval."""

    def post(self, request, request_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        try:
            join_request = self.requests().get(pk=request_id)
        except PatientJoinRequest.DoesNotExist, DjangoValidationError, ValueError:
            return Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        if not join_request.is_pending:
            # 409, not 404: the request WAS found. It has simply already been
            # decided, and saying so is more useful than a second meaning for
            # "not found".
            return Response(
                {"detail": f"This request was already {join_request.status}."},
                status=status.HTTP_409_CONFLICT,
            )

        join_request.status = PatientJoinRequest.STATUS_REJECTED
        join_request.decision_note = request.data.get("note", "")
        join_request.decided_at = timezone.now()
        join_request.decided_by = request.user
        join_request.save(update_fields=["status", "decision_note", "decided_at", "decided_by", "updated_at"])
        # Her account and profile survive — she can ask a different hospital.
        return Response(PatientJoinRequestSerializer(join_request).data)
