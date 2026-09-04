"""Patient and pregnancy endpoints.

Every queryset here is scoped through ``location__organization`` by the shared
tenancy mixin. Nothing accepts an organization or location from the client:
tenant membership is taken from the authenticated user, so there is no
identifier a caller could tamper with to reach another hospital's patients.
"""

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Concat
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsClinician, IsHospitalStaff
from momcare_platform.core.common.scoping import (
    OrganizationScopedQuerysetMixin,
    scope_to_assigned_staff,
)
from momcare_platform.core.patients.api.serializers import (
    CareTeamMembershipCreateSerializer,
    CareTeamMembershipSerializer,
    ClinicalNoteCreateSerializer,
    ClinicalNoteSerializer,
    ConsentInputSerializer,
    ConsentSerializer,
    PatientCreateSerializer,
    PatientDetailSerializer,
    PatientListSerializer,
    PregnancySerializer,
    PregnancyWriteSerializer,
    WorklistPatientSerializer,
)
from momcare_platform.core.patients.models import CareTeamMembership, ClinicalNote, Consent, Patient, Pregnancy
from momcare_platform.core.patients.services import EnrolmentError, create_pregnancy, enrol_patient

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
            return self.patients().select_related("location", "user").get(pk=patient_id), None
        except (Patient.DoesNotExist, DjangoValidationError, ValueError):
            return None, Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)


def _active_pregnancy_prefetch() -> Prefetch:
    """The active pregnancy for each listed patient, carrying its latest risk level.

    Two things are being avoided here. ``current_pregnancy`` queries once per
    patient, so a page of twenty costs twenty round trips; and reading the
    latest assessment through the related manager would cost twenty more. Both
    collapse into one prefetch with a correlated subquery.

    Imported inside the function: monitoring already imports patients, so a
    module-level import the other way would close the cycle.
    """
    from momcare_platform.core.monitoring.models import RiskAssessment

    latest = RiskAssessment.objects.filter(pregnancy=OuterRef("pk")).order_by("-assessed_at")

    active = (
        Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE)
        .annotate(
            latest_risk_level=Subquery(latest.values("level")[:1]),
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

        queryset = self.patients().select_related("location").prefetch_related(
            _active_pregnancy_prefetch(),
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

        # Context carries the request so the nested assigned_staff field can
        # narrow its queryset to this hospital's own clinicians.
        serializer = PatientCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        patient_data, pregnancy_data, risk_factors, consent = serializer.split()

        try:
            patient = enrol_patient(
                organization=org,
                recorded_by=request.user,
                patient_data=patient_data,
                pregnancy_data=pregnancy_data,
                risk_factor_data=risk_factors,
                consent=consent,
            )
        except EnrolmentError as exc:
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
    from "stable" everywhere else in this portal - see
    docs/worklist-feature-scope.md for the full reasoning.

    The two day thresholds below are administrative defaults, not clinically
    validated - the same honesty risk_rules.py already applies to its own
    thresholds. Worth revisiting alongside the obstetrician review
    (PLAN.md §3 item 3), not asserted as correct here.
    """

    organization_lookup = "patient__location__organization"

    NO_READING_AFTER = timedelta(days=7)
    NO_NOTE_AFTER = timedelta(days=30)

    def get(self, request):
        _, error = self.hospital_or_error(request)
        if error:
            return error

        # Function-local: monitoring already imports patients, so a
        # module-level import the other way would close the cycle (same
        # reasoning as _active_pregnancy_prefetch above).
        from momcare_platform.core.monitoring.models import VitalReading

        pregnancies = self.scope_to_organization(
            Pregnancy.objects.filter(status=Pregnancy.STATUS_ACTIVE),
        ).select_related("patient", "assigned_staff__user", "risk_factors")
        pregnancies = scope_to_assigned_staff(pregnancies, request, path_prefix="")

        latest_reading = VitalReading.objects.filter(pregnancy=OuterRef("pk")).order_by("-recorded_at")
        latest_note = ClinicalNote.objects.filter(pregnancy=OuterRef("pk")).order_by("-created_at")
        pregnancies = pregnancies.annotate(
            latest_reading_at=Subquery(latest_reading.values("recorded_at")[:1]),
            latest_note_at=Subquery(latest_note.values("created_at")[:1]),
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

        return Response(
            {
                "count": len(rows),
                "results": WorklistPatientSerializer(rows, many=True).data,
            },
        )

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

        note_gap = self._days_since(pregnancy.latest_note_at, now)
        if note_gap is None or note_gap >= self.NO_NOTE_AFTER.days:
            reasons.append(
                {
                    "code": "no_recent_note",
                    "detail": (
                        f"No clinical note in {note_gap} days."
                        if note_gap is not None
                        else "No clinical note has ever been logged."
                    ),
                    "days": note_gap,
                },
            )

        risk_factors = getattr(pregnancy, "risk_factors", None)
        if risk_factors is None:
            reasons.append(
                {
                    "code": "no_risk_history",
                    "detail": "No obstetric risk history has been recorded.",
                    "days": None,
                },
            )
        elif len(risk_factors.unanswered_factors) == len(risk_factors.FACTOR_FIELDS):
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

        pregnancies = patient.pregnancies.select_related("risk_factors", "assigned_staff__user")
        return Response(PregnancySerializer(pregnancies, many=True).data)

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
        risk_factors = data.pop("risk_factors", None)

        try:
            pregnancy = create_pregnancy(patient=patient, data=data, risk_factor_data=risk_factors)
        except EnrolmentError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PregnancySerializer(pregnancy).data, status=status.HTTP_201_CREATED)


class PregnancyDetailView(PatientScopedView):
    """Read or correct one pregnancy. Deliberately no DELETE — a pregnancy is
    historical clinical fact, corrected rather than removed."""

    def _get(self, patient, pregnancy_id):
        try:
            return patient.pregnancies.select_related("risk_factors").get(pk=pregnancy_id), None
        except (Pregnancy.DoesNotExist, DjangoValidationError, ValueError):
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


class PregnancyNotesView(PatientScopedView):
    """A pregnancy's clinical notes — append-only, newest first.

    Anyone on the hospital's staff can read them (an admin may need one for a
    liability review, same reasoning as alert visibility), but only a
    clinician can write one: this is a clinical judgement, not an admin task,
    the same split already drawn for acknowledging alerts and risk.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), IsClinician()]
        return [IsAuthenticated(), IsHospitalStaff()]

    def get(self, request, patient_id, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing
        try:
            pregnancy = patient.pregnancies.get(pk=pregnancy_id)
        except (Pregnancy.DoesNotExist, DjangoValidationError, ValueError):
            return Response({"detail": "Pregnancy not found."}, status=status.HTTP_404_NOT_FOUND)

        notes = pregnancy.clinical_notes.select_related("author__user")
        return Response(ClinicalNoteSerializer(notes, many=True).data)

    def post(self, request, patient_id, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing
        try:
            pregnancy = patient.pregnancies.get(pk=pregnancy_id)
        except (Pregnancy.DoesNotExist, DjangoValidationError, ValueError):
            return Response({"detail": "Pregnancy not found."}, status=status.HTTP_404_NOT_FOUND)

        # A clinician always has a Staff record - IsClinician already
        # confirmed the role, and every clinical role is invited as staff.
        author = request.user.staff

        serializer = ClinicalNoteCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = ClinicalNote.objects.create(
            pregnancy=pregnancy,
            author=author,
            **serializer.validated_data,
        )
        return Response(ClinicalNoteSerializer(note).data, status=status.HTTP_201_CREATED)


class PatientConsentView(PatientScopedView):
    """Record a further consent event — a withdrawal, or a re-grant.

    Append-only: earlier records are never altered, so the history of what was
    agreed and when survives intact.
    """

    def post(self, request, patient_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        serializer = ConsentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        consent = Consent.objects.create(
            patient=patient,
            recorded_by=request.user,
            **serializer.validated_data,
        )
        return Response(ConsentSerializer(consent).data, status=status.HTTP_201_CREATED)


def _can_manage_care_team(user, pregnancy) -> bool:
    """hospital_admin always can, org/location-wide. A care_manager can only
    when they hold an active ``care_manager`` membership on *this specific*
    pregnancy — never org-wide, and never inherited from any other case.

    Deliberately permissive on one point, decided explicitly rather than
    guessed at: a qualifying care_manager can add *another* care_manager to
    the same pregnancy, who then gets the same pregnancy-scoped authority.
    That is delegation within a case, not organization-wide escalation — the
    new member still can't touch any pregnancy they aren't themselves an
    active member of, which is exactly what this function checks on every
    call, not just at the moment they were added.
    """
    if user.role_code == settings.ROLE_HOSPITAL_ADMIN:
        return True
    if user.role_code != settings.ROLE_CARE_MANAGER:
        return False
    staff = getattr(user, "staff", None)
    if staff is None:
        return False
    return CareTeamMembership.objects.filter(
        pregnancy=pregnancy,
        staff=staff,
        role=CareTeamMembership.ROLE_CARE_MANAGER,
        is_active=True,
    ).exists()


class CareTeamMembershipListCreateView(PatientScopedView):
    """A pregnancy's care team — supporting members alongside its one lead
    clinician (``Pregnancy.assigned_staff``, untouched, read and written
    through the pregnancy endpoints exactly as before).

    Write access: hospital_admin, or a care_manager with an active
    membership on this specific pregnancy — see ``_can_manage_care_team``.
    Provider and nurse are read-only here, same as everyone else who isn't
    hospital staff at all is refused entirely by ``IsHospitalStaff``.
    """

    def _get_pregnancy(self, patient, pregnancy_id):
        try:
            return patient.pregnancies.get(pk=pregnancy_id), None
        except (Pregnancy.DoesNotExist, DjangoValidationError, ValueError):
            return None, Response({"detail": "Pregnancy not found."}, status=status.HTTP_404_NOT_FOUND)

    def get(self, request, patient_id, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing
        pregnancy, gone = self._get_pregnancy(patient, pregnancy_id)
        if gone:
            return gone

        memberships = pregnancy.care_team_memberships.select_related("staff__user").order_by("-started_at")
        return Response(CareTeamMembershipSerializer(memberships, many=True).data)

    def post(self, request, patient_id, pregnancy_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing
        pregnancy, gone = self._get_pregnancy(patient, pregnancy_id)
        if gone:
            return gone

        if not _can_manage_care_team(request.user, pregnancy):
            return Response(
                {"detail": "You do not have permission to manage this pregnancy's care team."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = CareTeamMembershipCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        try:
            membership = CareTeamMembership.objects.create(
                pregnancy=pregnancy,
                created_by=request.user,
                **serializer.validated_data,
            )
        except DjangoValidationError as exc:
            # The model's own clean()/save() guard (an already-deactivated
            # staff member can't be newly assigned) - surfaced the same way
            # DRF surfaces any other field error, not as a 500.
            return Response(exc.message_dict, status=status.HTTP_400_BAD_REQUEST)

        return Response(CareTeamMembershipSerializer(membership).data, status=status.HTTP_201_CREATED)


class CareTeamMembershipEndView(PatientScopedView):
    """End a membership — never delete it. Same authority boundary as
    creating one; see ``_can_manage_care_team`` and
    CareTeamMembershipListCreateView's docstring.

    Includes the case where the membership being ended is the acting
    care_manager's own: ending it is allowed (self-removal), and takes
    effect immediately — the very next write request against this
    pregnancy re-checks ``_can_manage_care_team`` from scratch and finds
    nothing, since authorization is never cached, only ever read fresh
    from the row this same request just changed.
    """

    def post(self, request, patient_id, pregnancy_id, membership_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

        try:
            pregnancy = patient.pregnancies.get(pk=pregnancy_id)
        except (Pregnancy.DoesNotExist, DjangoValidationError, ValueError):
            return Response({"detail": "Pregnancy not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _can_manage_care_team(request.user, pregnancy):
            return Response(
                {"detail": "You do not have permission to manage this pregnancy's care team."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            membership = CareTeamMembership.objects.get(
                pk=membership_id,
                pregnancy__patient=patient,
                pregnancy_id=pregnancy_id,
            )
        except (CareTeamMembership.DoesNotExist, DjangoValidationError, ValueError):
            return Response({"detail": "Care team membership not found."}, status=status.HTTP_404_NOT_FOUND)

        membership.end(by=request.user)
        return Response(CareTeamMembershipSerializer(membership).data)
