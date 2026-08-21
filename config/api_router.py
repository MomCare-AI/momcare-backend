from django.conf import settings
from django.urls import re_path
from rest_framework.routers import DefaultRouter, SimpleRouter

from momcare_platform.core.common.programs import iter_programs
from momcare_platform.core.organization.api.views import MyOrganizationView
from momcare_platform.core.staff.api.views import (
    InviteAcceptView,
    InviteDetailView,
    StaffInviteListCreateView,
    StaffInviteRevokeView,
    StaffListView,
)
from momcare_platform.core.users.api.auth import LoginView, LogoutView, MeView, RefreshView, RegisterView

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
]

core_urlpatterns = [
    re_path(r"^organization/me/?$", MyOrganizationView.as_view(), name="organization-me"),
    re_path(r"^staff/?$", StaffListView.as_view(), name="staff-list"),
    re_path(r"^staff/invites/?$", StaffInviteListCreateView.as_view(), name="staff-invite-list"),
    re_path(
        r"^staff/invites/(?P<invite_id>[0-9a-f-]{36})/revoke/?$",
        StaffInviteRevokeView.as_view(),
        name="staff-invite-revoke",
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
