from django.conf import settings
from django.urls import re_path
from rest_framework.routers import DefaultRouter, SimpleRouter

from momcare_platform.core.common.programs import iter_programs
from momcare_platform.core.locations.api.views import (
    LocationAssignmentStatusView,
    LocationDeactivateView,
    LocationDetailView,
    LocationListCreateView,
    LocationMovePatientsView,
    LocationPatientsView,
    LocationReactivateView,
)
from momcare_platform.core.monitoring.api.views import (
    ClinicalTagDetailView,
    ClinicalTagListCreateView,
    MonitoringNoteDetailView,
    MonitoringSessionDetailView,
    PatientMonitoringNotesView,
    PatientMonitoringSessionsView,
    PatientMonitoringView,
    PatientStatusDetailView,
    PatientStatusListCreateView,
    StatusLabelDetailView,
    StatusLabelListCreateView,
)
from momcare_platform.core.organization.api.views import (
    MyOrganizationView,
    NotificationMarkReadView,
    OrganizationAuditLogView,
    OrganizationConfidenceThresholdView,
    OrganizationDeactivationRequestView,
    OrganizationNotificationsView,
)
from momcare_platform.core.patients.api.views import (
    HospitalDirectoryView,
    JoinRequestDecisionView,
    JoinRequestReviewView,
    JoinRequestWithdrawView,
    PatientDeactivateView,
    PatientDetailView,
    PatientJoinRequestView,
    PatientListCreateView,
    PatientReactivateView,
    PatientWorklistView,
    PregnancyDetailView,
    PregnancyListCreateView,
)
from momcare_platform.core.staff.api.views import (
    SecondaryProviderDetailView,
    SecondaryProviderListCreateView,
    StaffAssignmentStatusView,
    StaffAuditReportView,
    StaffDeactivateView,
    StaffListView,
    StaffProfileView,
    StaffReactivateView,
)
from momcare_platform.core.users.api.auth import (
    ForgotPasswordView,
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    PatientRegisterView,
    RefreshView,
    RegisterView,
    ResendPatientVerificationView,
    ResetPasswordView,
    VerifyPatientEmailView,
    VerifyResetTokenView,
)

# Config wiring is allowed to import modules.pregnancy directly: routes are
# mounted explicitly here, by name, rather than through the ProgramSpec
# self-registration in core/common/programs.py — a deliberate call kept even
# after alerts/monitoring moved out of core (see CLAUDE.md's "structural
# divergence" section and the `project wiring must not hard-import modules`
# entry in pyproject.toml, updated the same day this moved).
from momcare_platform.modules.pregnancy.alerts.api.views import (
    AlertAcknowledgeView,
    AlertDetailView,
    AlertListView,
    AlertResolveView,
)
from momcare_platform.modules.pregnancy.vitals.api.views import (
    DeviceAssignView,
    DeviceListCreateView,
    LatestReadingsView,
    ReadingListCreateView,
    RiskAssessmentView,
    VerifyRiskView,
)

router = DefaultRouter() if settings.DEBUG else SimpleRouter()
# Make the trailing slash optional on every router-generated URL so the API
# responds identically to "/patients" and "/patients/".
router.trailing_slash = "/?"

# Core, always-on routes — register each ViewSet here once its app's
# api/views.py is actually implemented (currently stubs — see docs/design/).
# router.register("users", UserViewSet)
# router.register("patients", PatientViewSet)
# router.register("locations", LocationViewSet)
# router.register("staff", StaffViewSet)

# Program routes come from the registry — core never imports a module directly.
# Routes are mounted for every registered program; runtime activation is enforced
# per-request by the module gate (an inactive module returns 503), so URL loading
# never needs a database connection (safe during migrate / check / startup).
for spec in iter_programs():
    router.registry.extend(spec.router_factory().registry)  # type: ignore


app_name = "api"

# Explicit (non-viewset) endpoints — auth, roles, languages — get added here
# once core/users/api/auth.py is implemented, e.g.:
#
# auth_urlpatterns = [
#     re_path(r"^auth/login/?$", LoginView.as_view(), name="auth-login"),
#     re_path(r"^auth/refresh/?$", RefreshTokenView.as_view(), name="auth-refresh"),
#     re_path(r"^auth/logout/?$", LogoutView.as_view(), name="auth-logout"),
#     re_path(r"^auth/me/?$", MeView.as_view(), name="auth-me"),
# ]
# core_urlpatterns = [
#     re_path(r"^organization/?$", OrganizationView.as_view(), name="organization"),
# ]

auth_urlpatterns = [
    re_path(r"^auth/login/?$", LoginView.as_view(), name="auth-login"),
    re_path(r"^auth/refresh/?$", RefreshView.as_view(), name="auth-refresh"),
    re_path(r"^auth/logout/?$", LogoutView.as_view(), name="auth-logout"),
    re_path(r"^auth/me/?$", MeView.as_view(), name="auth-me"),
    # Passwords. Sensitive enough to be throttled harder than the rest.
    re_path(r"^auth/password/change/?$", PasswordChangeView.as_view(), name="password-change"),
    # Names match Neuro_RPM's own auth endpoints exactly (forgot-password ->
    # verify-reset-token -> reset-password), not an arbitrary MomCare scheme.
    re_path(r"^auth/forgot-password/?$", ForgotPasswordView.as_view(), name="auth-forgot-password"),
    re_path(
        r"^auth/verify-reset-token/?$",
        VerifyResetTokenView.as_view(),
        name="auth-verify-reset-token",
    ),
    re_path(r"^auth/reset-password/?$", ResetPasswordView.as_view(), name="auth-reset-password"),
    # A woman signing herself up from the mobile app. Creates a login with no
    # hospital attached — the Patient record only exists once a hospital
    # approves her join request.
    re_path(
        r"^auth/patient/register/?$",
        PatientRegisterView.as_view(),
        name="patient-register",
    ),
    re_path(
        r"^auth/patient/verify-email/?$",
        VerifyPatientEmailView.as_view(),
        name="patient-verify-email",
    ),
    re_path(
        r"^auth/patient/resend-verification/?$",
        ResendPatientVerificationView.as_view(),
        name="patient-resend-verification",
    ),
]

core_urlpatterns = [
    # Onboarding a hospital creates the tenant itself, not a session — a
    # different kind of action from everything in auth_urlpatterns above,
    # which all assume an organization already exists. Lives here, not under
    # /auth/, so the route name matches the domain concept everywhere else
    # calls this: organization onboarding.
    re_path(r"^organization/onboard/?$", RegisterView.as_view(), name="organization-onboard"),
    re_path(r"^organization/me/?$", MyOrganizationView.as_view(), name="organization-me"),
    re_path(
        r"^organization/me/confidence-threshold/?$",
        OrganizationConfidenceThresholdView.as_view(),
        name="organization-confidence-threshold",
    ),
    re_path(
        r"^organization/me/deactivation-request/?$",
        OrganizationDeactivationRequestView.as_view(),
        name="organization-deactivation-request",
    ),
    re_path(
        r"^organization/me/audit-log/?$",
        OrganizationAuditLogView.as_view(),
        name="organization-audit-log",
    ),
    # The bell icon -- "something needs your attention." No email, see
    # organization/signals.py for why and what currently produces one.
    re_path(
        r"^organization/me/notifications/?$",
        OrganizationNotificationsView.as_view(),
        name="organization-notifications",
    ),
    re_path(
        r"^organization/me/notifications/(?P<notification_id>[0-9a-f-]{36})/mark-read/?$",
        NotificationMarkReadView.as_view(),
        name="organization-notification-mark-read",
    ),
    # Locations — a hospital managing its own sites. Immediate-effect,
    # hospital_admin-gated actions; no platform-admin approval anywhere here.
    re_path(r"^locations/?$", LocationListCreateView.as_view(), name="location-list"),
    re_path(
        r"^locations/(?P<location_id>[0-9a-f-]{36})/?$",
        LocationDetailView.as_view(),
        name="location-detail",
    ),
    re_path(
        r"^locations/(?P<location_id>[0-9a-f-]{36})/assignment-status/?$",
        LocationAssignmentStatusView.as_view(),
        name="location-assignment-status",
    ),
    re_path(
        r"^locations/(?P<location_id>[0-9a-f-]{36})/deactivate/?$",
        LocationDeactivateView.as_view(),
        name="location-deactivate",
    ),
    re_path(
        r"^locations/(?P<location_id>[0-9a-f-]{36})/reactivate/?$",
        LocationReactivateView.as_view(),
        name="location-reactivate",
    ),
    re_path(
        r"^locations/(?P<location_id>[0-9a-f-]{36})/move-patients/?$",
        LocationMovePatientsView.as_view(),
        name="location-move-patients",
    ),
    re_path(
        r"^locations/(?P<location_id>[0-9a-f-]{36})/patients/?$",
        LocationPatientsView.as_view(),
        name="location-patients",
    ),
    # Secondary providers — external clinicians referenced on a patient's
    # record. Not platform staff: no login, no role. Their own resource
    # because one such clinician is shared across many patients.
    re_path(
        r"^secondary-providers/?$",
        SecondaryProviderListCreateView.as_view(),
        name="secondary-provider-list",
    ),
    re_path(
        r"^secondary-providers/(?P<provider_id>[0-9a-f-]{36})/?$",
        SecondaryProviderDetailView.as_view(),
        name="secondary-provider-detail",
    ),
    re_path(r"^staff/?$", StaffListView.as_view(), name="staff-list"),
    re_path(
        r"^staff/(?P<staff_id>[0-9a-f-]{36})/?$",
        StaffProfileView.as_view(),
        name="staff-detail",
    ),
    re_path(
        r"^staff/(?P<staff_id>[0-9a-f-]{36})/assignment-status/?$",
        StaffAssignmentStatusView.as_view(),
        name="staff-assignment-status",
    ),
    re_path(
        r"^staff/(?P<staff_id>[0-9a-f-]{36})/audit-report/?$",
        StaffAuditReportView.as_view(),
        name="staff-audit-report",
    ),
    re_path(
        r"^staff/(?P<staff_id>[0-9a-f-]{36})/deactivate/?$",
        StaffDeactivateView.as_view(),
        name="staff-deactivate",
    ),
    re_path(
        r"^staff/(?P<staff_id>[0-9a-f-]{36})/reactivate/?$",
        StaffReactivateView.as_view(),
        name="staff-reactivate",
    ),
    # Patients — mounted under /api/patients/ so AuditLogMiddleware's PHI
    # prefix already covers every mutation here.
    # Patient self-service — the mobile-app side. She has no organization, so
    # these run on an explicit self-filter rather than tenant scoping; see
    # PatientSelfView's docstring.
    #
    # "my-requests" (hers) and "patient-requests" (the hospital's queue) are
    # deliberately different words, not one nested under the other: two paths
    # that differ by a single segment read the same at a glance, and these are
    # two different people's endpoints with two different permission classes.
    re_path(r"^hospitals/?$", HospitalDirectoryView.as_view(), name="hospital-directory"),
    re_path(
        r"^my-requests/?$",
        PatientJoinRequestView.as_view(),
        name="my-requests",
    ),
    re_path(
        r"^my-requests/(?P<request_id>[0-9a-f-]{36})/withdraw/?$",
        JoinRequestWithdrawView.as_view(),
        name="my-request-withdraw",
    ),
    # The hospital's side: requests sent to it by patients.
    re_path(r"^patient-requests/?$", JoinRequestReviewView.as_view(), name="patient-request-list"),
    re_path(
        r"^patient-requests/(?P<request_id>[0-9a-f-]{36})/approve/?$",
        JoinRequestDecisionView.as_view(),
        {"decision": "approved"},
        name="patient-request-approve",
    ),
    re_path(
        r"^patient-requests/(?P<request_id>[0-9a-f-]{36})/reject/?$",
        JoinRequestDecisionView.as_view(),
        {"decision": "rejected"},
        name="patient-request-reject",
    ),
    re_path(r"^patients/?$", PatientListCreateView.as_view(), name="patient-list"),
    re_path(r"^patients/worklist/?$", PatientWorklistView.as_view(), name="patient-worklist"),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/?$",
        PatientDetailView.as_view(),
        name="patient-detail",
    ),
    # Deactivate, never delete — the same soft-deactivation Locations and
    # Staff already use.
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/deactivate/?$",
        PatientDeactivateView.as_view(),
        name="patient-deactivate",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/reactivate/?$",
        PatientReactivateView.as_view(),
        name="patient-reactivate",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/pregnancies/?$",
        PregnancyListCreateView.as_view(),
        name="pregnancy-list",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/?$",
        PregnancyDetailView.as_view(),
        name="pregnancy-detail",
    ),
    # Care team is three direct columns on Pregnancy (provider/nurse/
    # care_manager), read and written through the pregnancy endpoints above —
    # there is no separate care-team endpoint any more.
    # Monitoring — readings hang off a pregnancy, never a patient, because a
    # reading only means something in the context of gestational age.
    re_path(
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/readings/?$",
        ReadingListCreateView.as_view(),
        name="reading-list",
    ),
    re_path(
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/readings/latest/?$",
        LatestReadingsView.as_view(),
        name="reading-latest",
    ),
    re_path(
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/device/?$",
        DeviceAssignView.as_view(),
        name="pregnancy-device",
    ),
    re_path(r"^devices/?$", DeviceListCreateView.as_view(), name="device-list"),
    # Risk — assessments record transitions, not every reading.
    re_path(
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/risk/?$",
        RiskAssessmentView.as_view(),
        name="risk-assessments",
    ),
    re_path(
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/risk/(?P<assessment_id>[0-9a-f-]{36})/verify/?$",
        VerifyRiskView.as_view(),
        name="risk-verify",
    ),
    # Clinical contact logging -- hangs off a patient, not a pregnancy (see
    # core/monitoring/models.py's own docstring for why): a patient can
    # exist with no pregnancy yet, and a note logged between two pregnancies
    # has nowhere else to point.
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/monitoring/?$",
        PatientMonitoringView.as_view(),
        name="patient-monitoring",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/monitoring/sessions/?$",
        PatientMonitoringSessionsView.as_view(),
        name="patient-monitoring-sessions",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/monitoring/notes/?$",
        PatientMonitoringNotesView.as_view(),
        name="patient-monitoring-notes",
    ),
    # Flat by id -- same reasoning as /alerts/<id>/ being flat even though
    # an Alert hangs off a Pregnancy.
    re_path(
        r"^monitoring-sessions/(?P<session_id>[0-9a-f-]{36})/?$",
        MonitoringSessionDetailView.as_view(),
        name="monitoring-session-detail",
    ),
    re_path(
        r"^monitoring-notes/(?P<note_id>[0-9a-f-]{36})/?$",
        MonitoringNoteDetailView.as_view(),
        name="monitoring-note-detail",
    ),
    # The tag catalogue notes draw from -- org-level (hospital-wide) or
    # location-level, see ClinicalTag's own docstring.
    re_path(r"^clinical-tags/?$", ClinicalTagListCreateView.as_view(), name="clinical-tag-list"),
    re_path(
        r"^clinical-tags/(?P<tag_id>[0-9a-f-]{36})/?$",
        ClinicalTagDetailView.as_view(),
        name="clinical-tag-detail",
    ),
    # The status-label catalogue -- org-level (hospital-wide) or
    # location-level, see StatusLabel's own docstring. Decoupled from
    # PatientStatus below (a picker source, not a foreign key).
    re_path(r"^status-labels/?$", StatusLabelListCreateView.as_view(), name="status-label-list"),
    re_path(
        r"^status-labels/(?P<label_id>[0-9a-f-]{36})/?$",
        StatusLabelDetailView.as_view(),
        name="status-label-detail",
    ),
    # A patient's status timeline -- nested under the patient, same shape as
    # monitoring notes above.
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/statuses/?$",
        PatientStatusListCreateView.as_view(),
        name="patient-status-list",
    ),
    re_path(
        r"^patient-statuses/(?P<status_id>[0-9a-f-]{36})/?$",
        PatientStatusDetailView.as_view(),
        name="patient-status-detail",
    ),
    # The queue a clinician works from.
    # Aggregates for the portal overview.
    # Alerts — the push side of the same information.
    re_path(r"^alerts/?$", AlertListView.as_view(), name="alert-list"),
    re_path(
        r"^alerts/(?P<alert_id>[0-9a-f-]{36})/?$",
        AlertDetailView.as_view(),
        name="alert-detail",
    ),
    re_path(
        r"^alerts/(?P<alert_id>[0-9a-f-]{36})/acknowledge/?$",
        AlertAcknowledgeView.as_view(),
        name="alert-acknowledge",
    ),
    re_path(
        r"^alerts/(?P<alert_id>[0-9a-f-]{36})/resolve/?$",
        AlertResolveView.as_view(),
        name="alert-resolve",
    ),
    # Platform admin API — deferred. core/platform_admin/ exists as an empty
    # skeleton (see CLAUDE.md); no routes until it's actually implemented.
    # Reviewing organizations/deactivation requests goes through Django
    # admin (OrganizationAdmin, OrganizationDeactivationRequestAdmin) until then.
]

urlpatterns = router.urls + auth_urlpatterns + core_urlpatterns
