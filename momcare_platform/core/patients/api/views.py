"""Patient and pregnancy endpoints.

Every queryset here is scoped through ``location__organization`` by the shared
tenancy mixin. Nothing accepts an organization or location from the client:
tenant membership is taken from the authenticated user, so there is no
identifier a caller could tamper with to reach another hospital's patients.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Concat
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsHospitalStaff, IsPatient
from momcare_platform.core.common.rls import bypass_rls
from momcare_platform.core.common.scoping import (
    OrganizationScopedQuerysetMixin,
    scope_to_assigned_staff,
)
from momcare_platform.core.patients.api.serializers import (
    PatientCreateSerializer,
    PatientDetailSerializer,
    PatientDraftSerializer,
    PatientJoinRequestSerializer,
    PatientListSerializer,
    PregnancySerializer,
    PregnancyWriteSerializer,
    WorklistPatientSerializer,
)
from momcare_platform.core.patients.models import Patient, PatientJoinRequest, Pregnancy
from momcare_platform.core.patients.services import (
    OnboardingError,
    create_pregnancy,
    deactivate_patient,
    onboard_patient,
    reactivate_patient,
)

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

    active = (
        Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE)
        .annotate(
            # final_risk_level: the level actually acted on, not the model's
            # pre-escalation answer.
            latest_risk_level=Subquery(latest.values("final_risk_level")[:1]),
            latest_risk_at=Subquery(latest.values("assessed_at")[:1]),
        )
        .order_by("-created_at")
    )
    return Prefetch("pregnancies", queryset=active, to_attr="active_pregnancies")


class PatientListCreateView(PatientScopedView):
    """List and search this hospital's patients, or enrol a new one."""

    def _scope_to_assigned(self, queryset, request):
        """``?assigned_to=me`` on a Patient queryset — see
        ``scope_to_assigned_staff``'s own docstring (core/common/scoping.py)
        for the shared semantics this delegates to.
        """
        return scope_to_assigned_staff(queryset, request, path_prefix="pregnancies__")

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        queryset = (
            self.patients()
            .select_related("location")
            .prefetch_related(
                _active_pregnancy_prefetch(),
            )
        )
        queryset = self._scope_to_assigned(queryset, request)

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
                | Q(cnic__icontains=search)
                | Q(mrn__icontains=search),
            )

        paginator = DefaultPagination()
        page = paginator.paginate_queryset(queryset.order_by("-created_at", "id"), request, view=self)
        return paginator.get_paginated_response(PatientListSerializer(page, many=True).data)

    def post(self, request):
        org, error = self.hospital_or_error(request)
        if error:
            return error

        # Context carries the request so the nested care-team fields can
        # narrow its queryset to this hospital's own clinicians.
        serializer = PatientCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        patient_data, pregnancy_data = serializer.split()

        try:
            patient = onboard_patient(
                organization=org,
                patient_data=patient_data,
                pregnancy_data=pregnancy_data,
            )
        except OnboardingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PatientDetailSerializer(patient).data, status=status.HTTP_201_CREATED)


class PatientWorklistView(PatientScopedView):
    """Administrative and care-continuity gaps — a different question from
    the Attention Queue's clinical severity.

    The Attention Queue (core/monitoring/api/views.py) answers "whose vitals
    just crossed a threshold." This answers "does this case have a gap that
    has nothing to do with today's vitals being bad" - no reading in a
    while, no note logged, no risk history ever answered, nobody
    accountable. Deliberately a separate endpoint and never merged with the
    Attention Queue, the same way "not assessed" stays visually distinct
    from "low risk" everywhere else in this portal - see
    docs/worklist-feature-scope.md for the full reasoning.

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
    """Deactivate a patient — never delete. Clinical records survive."""

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
    """Undo a deactivation."""

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

            page = paginator.paginate_queryset(hospitals.order_by("name", "id"), request, view=self)
            rows = [
                {
                    "id": str(h.id),
                    "name": h.name,
                    "city": h.city,
                    "country": h.country,
                    "phone": h.phone,
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

        draft = PatientDraftSerializer(data=request.data.get("draft") or {})
        draft.is_valid(raise_exception=True)

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
                # Store the raw submitted JSON, not validated_data — dates come
                # back as date objects, which JSONField cannot hold. It has
                # already passed validation above, and is re-validated at
                # approval time before anything is created from it.
                draft=request.data.get("draft") or {},
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


class JoinRequestBaseView(PatientScopedView):
    """Scoping shared by the hospital's queue and its approve/reject actions.

    Deliberately a sibling base rather than the decision view subclassing the
    queue view: their URLs pass different kwargs (``request_id``/``decision``
    vs none), so inheriting the queue's ``get`` made ``GET`` on
    ``/patient-requests/<id>/approve/`` raise TypeError and return 500 instead
    of a clean 405.
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

        rows = [
            {
                **PatientJoinRequestSerializer(r).data,
                "applicant_email": r.user.email,
                "applicant_name": r.user.get_full_name(),
            }
            for r in queryset.order_by("-created_at")
        ]
        return Response({"count": len(rows), "results": rows})


class JoinRequestDecisionView(JoinRequestBaseView):
    """Approve or reject one request."""

    def get_request_or_404(self, request_id):
        try:
            return self.requests().get(pk=request_id), None
        except PatientJoinRequest.DoesNotExist, DjangoValidationError, ValueError:
            return None, Response({"detail": "Request not found."}, status=status.HTTP_404_NOT_FOUND)

    def _decide(self, join_request, request, *, new_status, patient=None):
        join_request.status = new_status
        join_request.patient = patient
        join_request.decision_note = request.data.get("note", "")
        join_request.decided_at = timezone.now()
        join_request.decided_by = request.user
        join_request.save(
            update_fields=["status", "patient", "decision_note", "decided_at", "decided_by", "updated_at"],
        )

    def post(self, request, request_id, decision):
        org, error = self.hospital_or_error(request)
        if error:
            return error
        join_request, missing = self.get_request_or_404(request_id)
        if missing:
            return missing
        if not join_request.is_pending:
            # 409, not 404: the request WAS found. It has simply already been
            # decided, and saying so is more useful than a second meaning for
            # "not found".
            return Response(
                {"detail": f"This request was already {join_request.status}."},
                status=status.HTTP_409_CONFLICT,
            )

        if decision == PatientJoinRequest.STATUS_REJECTED:
            self._decide(join_request, request, new_status=PatientJoinRequest.STATUS_REJECTED)
            # Her account and draft survive — she can ask a different hospital.
            return Response(PatientJoinRequestSerializer(join_request).data)

        # She may have asked several hospitals at once; the first to approve
        # gets her. Patient.user is a one-to-one, so a second approval would
        # otherwise hit an IntegrityError and surface as a 500 — 409 says the
        # true thing instead: the request is fine, her situation has changed.
        #
        # bypass_rls for the same reason as the withdrawal below: the record
        # that already claims her belongs to a DIFFERENT hospital, so a scoped
        # read cannot see it. Without this the guard would never fire in
        # production and the 500 would come straight back — and no test could
        # show it, because local and test databases bypass RLS anyway.
        with bypass_rls():
            already = Patient.objects.filter(user=join_request.user).first()
        if already is not None:
            return Response(
                {
                    "detail": (
                        "This applicant has already been accepted by another hospital and is under their care."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Approval re-validates her draft through the same serializer that
        # accepted it, then creates the record through the same
        # onboard_patient() a walk-in uses. One creation path, so a
        # self-registered patient and a walk-in are the same kind of record.
        draft = PatientDraftSerializer(data=join_request.draft or {})
        if not draft.is_valid():
            return Response(
                {"detail": "This applicant's details are no longer valid.", "errors": draft.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        patient_data, pregnancy_data = draft.split()

        try:
            patient = onboard_patient(
                organization=org,
                patient_data=patient_data,
                pregnancy_data=pregnancy_data,
            )
        except OnboardingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        # Link her login to the clinical record now that one exists.
        patient.user = join_request.user
        patient.save(update_fields=["user", "updated_at"])

        self._decide(join_request, request, new_status=PatientJoinRequest.STATUS_APPROVED, patient=patient)

        # Close her remaining open requests. WITHDRAWN, not REJECTED — those
        # hospitals never said no, and a record claiming they did would be a
        # lie about a decision nobody made.
        #
        # bypass_rls is required, not incidental: these rows belong to OTHER
        # hospitals, and this session is scoped to this one, so the fail-closed
        # policy would match zero rows and silently withdraw nothing. Local and
        # test databases use a BYPASSRLS role, so no test can catch that —
        # it would only ever have shown up in production, as her other requests
        # staying pending forever against a woman already under someone's care.
        with bypass_rls():
            PatientJoinRequest.objects.filter(
                user=join_request.user,
                status=PatientJoinRequest.STATUS_PENDING,
            ).exclude(pk=join_request.pk).update(
                status=PatientJoinRequest.STATUS_WITHDRAWN,
                decided_at=timezone.now(),
                decision_note="Withdrawn automatically — she was accepted by another hospital.",
            )

        return Response(
            {
                **PatientJoinRequestSerializer(join_request).data,
                "patient": PatientDetailSerializer(patient).data,
            },
        )
