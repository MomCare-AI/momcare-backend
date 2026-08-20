from django.conf import settings
from rest_framework.routers import DefaultRouter, SimpleRouter

from momcare_platform.core.common.programs import iter_programs

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

urlpatterns = router.urls
