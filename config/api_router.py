from django.conf import settings
from django.urls import re_path
from rest_framework.routers import DefaultRouter, SimpleRouter

from momcare_platform.core.alerts.api.views import (
    AlertAcknowledgeView,
    AlertDetailView,
    AlertListView,
    AlertResolveView,
)
from momcare_platform.core.common.programs import iter_programs
from momcare_platform.core.monitoring.api.views import (
    AcknowledgeRiskView,
    AttentionQueueView,
    DeviceAssignView,
    DeviceListCreateView,
    LatestReadingsView,
    ReadingListCreateView,
    RiskAssessmentView,
    SimulateReadingsView,
)
from momcare_platform.core.organization.api.dashboard import DashboardSummaryView
from momcare_platform.core.organization.api.views import MyOrganizationView
from momcare_platform.core.patients.api.views import (
    CareTeamMembershipEndView,
    CareTeamMembershipListCreateView,
    PatientConsentView,
    PatientDetailView,
    PatientListCreateView,
    PatientWorklistView,
    PregnancyDetailView,
    PregnancyListCreateView,
    PregnancyNotesView,
)
from momcare_platform.core.staff.api.views import (
    InviteAcceptView,
    InviteDetailView,
    StaffInviteListCreateView,
    StaffInviteRevokeView,
    StaffListView,
    StaffProfileView,
)
from momcare_platform.core.users.api.auth import (
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RefreshView,
    RegisterView,
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
    re_path(r"^auth/register/?$", RegisterView.as_view(), name="auth-register"),
    re_path(r"^auth/login/?$", LoginView.as_view(), name="auth-login"),
    re_path(r"^auth/refresh/?$", RefreshView.as_view(), name="auth-refresh"),
    re_path(r"^auth/logout/?$", LogoutView.as_view(), name="auth-logout"),
    re_path(r"^auth/me/?$", MeView.as_view(), name="auth-me"),
    # Passwords. Sensitive enough to be throttled harder than the rest.
    re_path(r"^auth/password/change/?$", PasswordChangeView.as_view(), name="password-change"),
    re_path(r"^auth/password/reset/?$", PasswordResetRequestView.as_view(), name="password-reset"),
    re_path(
        r"^auth/password/reset/confirm/?$",
        PasswordResetConfirmView.as_view(),
        name="password-reset-confirm",
    ),
]

core_urlpatterns = [
    re_path(r"^organization/me/?$", MyOrganizationView.as_view(), name="organization-me"),
    re_path(r"^staff/?$", StaffListView.as_view(), name="staff-list"),
    re_path(
        r"^staff/(?P<staff_id>[0-9a-f-]{36})/?$",
        StaffProfileView.as_view(),
        name="staff-detail",
    ),
    re_path(r"^staff/invites/?$", StaffInviteListCreateView.as_view(), name="staff-invite-list"),
    re_path(
        r"^staff/invites/(?P<invite_id>[0-9a-f-]{36})/revoke/?$",
        StaffInviteRevokeView.as_view(),
        name="staff-invite-revoke",
    ),
    # Patients — mounted under /api/patients/ so AuditLogMiddleware's PHI
    # prefix already covers every mutation here.
    re_path(r"^patients/?$", PatientListCreateView.as_view(), name="patient-list"),
    re_path(r"^patients/worklist/?$", PatientWorklistView.as_view(), name="patient-worklist"),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/?$",
        PatientDetailView.as_view(),
        name="patient-detail",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/consent/?$",
        PatientConsentView.as_view(),
        name="patient-consent",
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
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/notes/?$",
        PregnancyNotesView.as_view(),
        name="pregnancy-notes",
    ),
    # Care team — supporting members alongside Pregnancy.assigned_staff (the
    # lead clinician, read/written through the pregnancy endpoints above,
    # untouched by any of this).
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/care-team/?$",
        CareTeamMembershipListCreateView.as_view(),
        name="care-team-list",
    ),
    re_path(
        r"^patients/(?P<patient_id>[0-9a-f-]{36})/pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})"
        r"/care-team/(?P<membership_id>[0-9a-f-]{36})/end/?$",
        CareTeamMembershipEndView.as_view(),
        name="care-team-end",
    ),
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
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/readings/simulate/?$",
        SimulateReadingsView.as_view(),
        name="reading-simulate",
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
        r"^pregnancies/(?P<pregnancy_id>[0-9a-f-]{36})/risk/(?P<assessment_id>[0-9a-f-]{36})/acknowledge/?$",
        AcknowledgeRiskView.as_view(),
        name="risk-acknowledge",
    ),
    # The queue a clinician works from.
    re_path(r"^attention/?$", AttentionQueueView.as_view(), name="attention-queue"),
    # Aggregates for the portal overview.
    re_path(r"^dashboard/summary/?$", DashboardSummaryView.as_view(), name="dashboard-summary"),
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
    # Public — the recipient holds only the token.
    re_path(r"^invites/(?P<token>[A-Za-z0-9_-]+)/?$", InviteDetailView.as_view(), name="invite-detail"),
    re_path(
        r"^invites/(?P<token>[A-Za-z0-9_-]+)/accept/?$",
        InviteAcceptView.as_view(),
        name="invite-accept",
    ),
]

urlpatterns = router.urls + auth_urlpatterns + core_urlpatterns
