"""Patient and pregnancy endpoints.

Every queryset here is scoped through ``location__organization`` by the shared
tenancy mixin. Nothing accepts an organization or location from the client:
tenant membership is taken from the authenticated user, so there is no
identifier a caller could tamper with to reach another hospital's patients.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import OuterRef, Prefetch, Q, Subquery, Value
from django.db.models.functions import Concat
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from momcare_platform.core.common.pagination import DefaultPagination
from momcare_platform.core.common.permissions import IsClinician, IsHospitalAdmin, IsHospitalStaff
from momcare_platform.core.common.scoping import OrganizationScopedQuerysetMixin
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
        """``?assigned_to=me`` — a clinician's honest "my patients", not the
        whole hospital wearing that label.

        Providers get both paths deliberately, not assigned_staff alone: a
        supporting/co-provider on a pregnancy is a real CareTeamMembership
        row, never the lead field, and would otherwise be invisible in their
        own "my patients" view despite genuinely being on the case. Nurses
        and care managers only ever exist as membership rows - there is no
        equivalent lead field for either.
        """
        if request.query_params.get("assigned_to") != "me":
            return queryset

        staff = getattr(request.user, "staff", None)
        if staff is None:
            return queryset.none()

        role = request.user.role_code
        if role == "provider":
            return queryset.filter(
                Q(pregnancies__assigned_staff=staff)
                | Q(
                    pregnancies__care_team_memberships__staff=staff,
                    pregnancies__care_team_memberships__role="provider",
                    pregnancies__care_team_memberships__is_active=True,
                ),
            ).distinct()
        if role in ("nurse", "care_manager"):
            return queryset.filter(
                pregnancies__care_team_memberships__staff=staff,
                pregnancies__care_team_memberships__role=role,
                pregnancies__care_team_memberships__is_active=True,
            ).distinct()
        # hospital_admin and anyone else: "my patients" isn't a concept that
        # applies to them - an empty, honest result rather than silently
        # ignoring the param and returning everyone under a label that
        # would be wrong for this role specifically.
        return queryset.none()

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


class CareTeamMembershipListCreateView(PatientScopedView):
    """A pregnancy's care team — supporting members alongside its one lead
    clinician (``Pregnancy.assigned_staff``, untouched, read and written
    through the pregnancy endpoints exactly as before).

    Write access is ``IsHospitalAdmin`` only for now. The dashboard master
    plan's own recommendation was hospital_admin **and** care_manager for
    their own coordinated cases — deliberately not implemented here, because
    "how does a case become a care_manager's own to coordinate" is still an
    open product decision (see the plan's open-decisions list), not
    something to guess at in the permission check.
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), IsHospitalAdmin()]
        return [IsAuthenticated(), IsHospitalStaff()]

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
    creating one; see CareTeamMembershipListCreateView's docstring."""

    permission_classes = [IsAuthenticated, IsHospitalAdmin]

    def post(self, request, patient_id, pregnancy_id, membership_id):
        _, error = self.hospital_or_error(request)
        if error:
            return error
        patient, missing = self.get_patient_or_404(patient_id)
        if missing:
            return missing

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
